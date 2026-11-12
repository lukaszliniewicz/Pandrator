"""Editing and regenerating during an immutable run must not lose audio."""
import threading
from unittest.mock import patch

import pytest
from pydub import AudioSegment
from sqlalchemy import event, select, text

from pandrator.web.generation_edit_audio import capture_selection_guards
from pandrator.web.generation_run_selection import resolve_generation_run_take_ids
from pandrator.web.jobs import Worker
from pandrator.web.models import (
    Artifact,
    AudioTake,
    GenerationPlanRevision,
    GenerationRun,
    GenerationSegment,
    Job,
    OutputAssembly,
)
from pandrator.web.projects.artifact_identity import resolve_artifact_ownership
from pandrator.web.workflow_generation_finalization import finalize_generation_run


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


def test_capture_selection_guards_uses_narrow_take_queries(case):
    _foreign_session_id, foreign_ids = case._create_session_with_plan()
    with case.database.session() as session:
        revision_id = session.get(GenerationSegment, case.segment_ids[0]).plan_revision_id
        session.get(GenerationSegment, case.segment_ids[2]).removed = True
        ownership = resolve_artifact_ownership(session)
        active_artifact = Artifact(
            workspace_id=ownership.workspace_id,
            project_id=ownership.project_id,
            kind="audio",
            relative_path="guards-active.wav",
        )
        inactive_artifact = Artifact(
            workspace_id=ownership.workspace_id,
            project_id=ownership.project_id,
            kind="audio",
            relative_path="guards-inactive.wav",
        )
        session.add_all((active_artifact, inactive_artifact))
        session.flush()
        session.add_all((
            AudioTake(
                generation_segment_id=case.segment_ids[0],
                artifact_id=active_artifact.id, is_active=True, revision=7,
            ),
            AudioTake(
                generation_segment_id=case.segment_ids[0],
                artifact_id=inactive_artifact.id, is_active=False, revision=3,
            ),
            AudioTake(
                generation_segment_id=case.segment_ids[2],
                artifact_id=inactive_artifact.id, is_active=True, revision=9,
            ),
            AudioTake(
                generation_segment_id=foreign_ids[0],
                artifact_id=inactive_artifact.id, is_active=True, revision=11,
            ),
        ))

    statements = []

    def capture_sql(_connection, _cursor, statement, _parameters, _context, _many):
        if statement.lstrip().upper().startswith("SELECT"):
            statements.append(" ".join(statement.split()))

    event.listen(case.database.engine, "before_cursor_execute", capture_sql)
    try:
        with case.database.session() as session:
            guards = capture_selection_guards(session, revision_id)
    finally:
        event.remove(case.database.engine, "before_cursor_execute", capture_sql)

    assert guards == {
        case.segment_ids[0]: {"artifact_id": active_artifact.id, "revision": 7},
        case.segment_ids[1]: {"artifact_id": None, "revision": None},
        case.segment_ids[2]: {"artifact_id": inactive_artifact.id, "revision": 9},
    }
    assert len(statements) == 2
    segment_query, take_query = statements
    assert segment_query.split(" FROM ", 1)[0] == "SELECT generation_segments.id"
    assert take_query.split(" FROM ", 1)[0] == (
        "SELECT audio_takes.generation_segment_id, audio_takes.artifact_id, "
        "audio_takes.revision, audio_takes.is_active"
    )
    assert "FROM audio_takes" in take_query
    assert "JOIN" not in take_query.upper()
    assert "audio_takes.generation_segment_id IN" in take_query
    assert "audio_takes.is_active" not in take_query.split(" WHERE ", 1)[1]


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
    resumed_first = case._resume_run(first["id"])
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

    resumed = case._resume_run(child["id"])
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

    resumed = case._resume_run(child["id"])
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


def test_standalone_partial_run_pins_guarded_audio_through_later_selection(case):
    original = case._start()
    execute(case, original)
    edited = edit(case, 0, "Edited one")
    current = page(case)
    partial = case._start(
        operation="regenerate",
        segment_ids=[edited["id"]],
        speech_plan_revision_id=current["plan_revision_id"],
    )
    result, calls = execute(case, partial, [edited["id"]], "regenerate")
    assert result["generated"] == 1
    assert calls == ["Edited one"]

    generation = case.app.extensions["pandrator"]["generation"]
    handlers = case.app.extensions["pandrator"]["workflow_handlers"]
    with case.database.session() as session:
        selected_run = session.get(GenerationRun, partial["id"])
        selected_take_ids = resolve_generation_run_take_ids(session, selected_run)
        requested_ids = selected_run.settings_snapshot_json[
            "generation_request_segment_ids"
        ]
        guards = selected_run.settings_snapshot_json["generation_selection_guards"]
        assert set(selected_take_ids) == set(current_row["id"] for current_row in current["items"])
        assert set(requested_ids) == {edited["id"]}
        assert session.scalar(
            select(AudioTake.id).where(
                AudioTake.id == selected_take_ids[edited["id"]],
                AudioTake.generation_run_id == partial["id"],
            )
        )

        untouched_id = next(
            row["id"] for row in current["items"] if row["id"] != edited["id"]
        )
        guard_artifact_id = guards[untouched_id]["artifact_id"]
        guarded_clone = session.scalar(
            select(AudioTake)
            .where(
                AudioTake.generation_segment_id == untouched_id,
                AudioTake.artifact_id == guard_artifact_id,
                AudioTake.generation_run_id.is_(None),
                AudioTake.status.in_(("completed", "stale")),
            )
            .order_by(AudioTake.created_at.desc(), AudioTake.id.desc())
        )
        assert guarded_clone is not None
        guarded_clone.revision += 10
        for take in session.scalars(
            select(AudioTake).where(AudioTake.generation_segment_id == untouched_id)
        ):
            take.is_active = False

        later_run = GenerationRun(
            session_id=selected_run.session_id,
            plan_revision_id=selected_run.plan_revision_id,
            sequence_number=selected_run.sequence_number + 1,
            status="completed",
        )
        ownership = resolve_artifact_ownership(session, session_id=selected_run.session_id)
        later_artifact = Artifact(
            session_id=selected_run.session_id,
            workspace_id=ownership.workspace_id,
            project_id=ownership.project_id,
            kind="audio",
            relative_path="later-selection.wav",
        )
        session.add_all((later_run, later_artifact))
        session.flush()
        later_take = AudioTake(
            generation_segment_id=untouched_id,
            generation_run_id=later_run.id,
            artifact_id=later_artifact.id,
            status="completed",
            is_active=True,
        )
        session.add(later_take)
        session.flush()
        frozen_ids = dict(selected_take_ids)
        later_take_id = later_take.id

    selected_page = generation.list_segments(
        case.session_id,
        generation_run_id=partial["id"],
        take_history="selected",
    )
    effective_ids = {
        item["id"]: item["effective_take"]["id"]
        for item in selected_page["items"]
        if item["effective_take"] is not None
    }
    assert effective_ids == frozen_ids
    untouched_item = next(
        item for item in selected_page["items"] if item["id"] == untouched_id
    )
    assert untouched_item["active_take_id"] == later_take_id
    assert untouched_item["history_take_id"] == frozen_ids[untouched_id]

    queued = generation.create_assembly(
        case.session_id,
        generation_run_id=partial["id"],
        run_override={"output": {"format": "wav"}},
    )
    assembled = handlers.assemble_generation_output(
        {"output_assembly_id": queued["id"]},
        lambda *_args: None,
        threading.Event(),
    )
    assert assembled["artifact_id"]
    with case.database.session() as session:
        assembly = session.get(OutputAssembly, queued["id"])
        assert assembly.status == "completed"
        manifest_ids = {
            entry["segment_id"]: entry["take_id"]
            for entry in assembly.settings_json["takes"]
        }
    assert manifest_ids == frozen_ids


def test_failed_requested_regeneration_is_missing_and_finalizes_partial(case):
    original = case._start()
    execute(case, original)
    edited = edit(case, 0, "Edited one")
    current = page(case)
    partial = case._start(
        operation="regenerate",
        segment_ids=[edited["id"]],
        speech_plan_revision_id=current["plan_revision_id"],
    )

    with case.database.session() as session:
        run = session.get(GenerationRun, partial["id"])
        requested_id = run.settings_snapshot_json["generation_request_segment_ids"][0]
        guards = run.settings_snapshot_json["generation_selection_guards"]
        assert guards[requested_id]["artifact_id"]
        selected = resolve_generation_run_take_ids(session, run)
        assert requested_id not in selected
        assert set(selected) == {
            row["id"] for row in current["items"] if row["id"] != requested_id
        }
        for segment_id in selected:
            session.get(GenerationSegment, segment_id).status = "completed"
        session.get(GenerationSegment, requested_id).status = "completed"

    final_status, incomplete = finalize_generation_run(
        case.database,
        run_id=partial["id"],
        output_run_id=partial["id"],
        plan_revision_id=current["plan_revision_id"],
        operation="regenerate",
        repair_requested=False,
    )
    assert (final_status, incomplete) == ("partial", 1)


def test_partial_run_ignores_cross_plan_and_unguarded_clones(case):
    original = case._start()
    execute(case, original)
    edited = edit(case, 0, "Edited one")
    current = page(case)
    partial = case._start(
        operation="regenerate",
        segment_ids=[edited["id"]],
        speech_plan_revision_id=current["plan_revision_id"],
    )
    foreign_session_id, foreign_segment_ids = case._create_session_with_plan()

    with case.database.session() as session:
        run = session.get(GenerationRun, partial["id"])
        snapshot = dict(run.settings_snapshot_json)
        guards = dict(snapshot["generation_selection_guards"])
        requested_id = snapshot["generation_request_segment_ids"][0]
        untouched_ids = [
            row["id"] for row in current["items"] if row["id"] != requested_id
        ]
        cross_plan_target, unguarded_target = untouched_ids
        ownership = resolve_artifact_ownership(session, session_id=foreign_session_id)
        foreign_artifact = Artifact(
            session_id=foreign_session_id,
            workspace_id=ownership.workspace_id,
            project_id=ownership.project_id,
            kind="audio",
            relative_path="foreign-baseline.wav",
        )
        session.add(foreign_artifact)
        session.flush()
        session.add(
            AudioTake(
                generation_segment_id=foreign_segment_ids[0],
                artifact_id=foreign_artifact.id,
                status="completed",
                is_active=True,
            )
        )
        guards[cross_plan_target] = {"artifact_id": foreign_artifact.id, "revision": 1}
        guards.pop(unguarded_target)
        run.settings_snapshot_json = {
            **snapshot,
            "generation_selection_guards": guards,
        }
        selected = resolve_generation_run_take_ids(session, run)

    assert selected == {}


def _reference_inherit_edit_copy_audio(session, revision_id):
    """The reconcile as it was before candidates were selected in SQL."""
    from pandrator.web.generation_edit_audio import _copy_take, edit_copy_ancestors

    revision = session.get(GenerationPlanRevision, revision_id)
    if revision is None:
        return 0
    ancestors = edit_copy_ancestors(session, revision)
    if not ancestors:
        return 0
    targets = {row.ordinal: row for row in session.scalars(
        select(GenerationSegment).where(GenerationSegment.plan_revision_id == revision_id)
    )}
    existing_by_id = {}
    for take in session.scalars(select(AudioTake).join(
        GenerationSegment, GenerationSegment.id == AudioTake.generation_segment_id
    ).where(GenerationSegment.plan_revision_id == revision_id)):
        existing_by_id.setdefault(take.generation_segment_id, []).append(take)
    rows = session.execute(
        select(GenerationSegment, AudioTake, Artifact)
        .join(AudioTake, AudioTake.generation_segment_id == GenerationSegment.id)
        .join(Artifact, Artifact.id == AudioTake.artifact_id)
        .where(GenerationSegment.plan_revision_id.in_(ancestors),
               AudioTake.status.in_(("completed", "stale")))
        .order_by(AudioTake.created_at.desc(), AudioTake.id)
    ).all()
    changed = 0
    for source, take, artifact in rows:
        target = targets.get(source.ordinal)
        if target is not None:
            changed += _copy_take(session, source, target, take, artifact,
                                  existing_by_id.setdefault(target.id, []))
    if changed:
        session.flush()
    return changed


class _Rollback(Exception):
    pass


def _build_edit_copy_with_two_ancestors(case, count=10, *, declined_targets=True):
    """Edit copies R0 -> R1 -> R2 with hand-made takes covering every reconcile case."""
    from datetime import datetime, timedelta

    created = case.client.post(
        "/api/v1/sessions", json={"name": "Reconcile", "workflow_kind": "audiobook"},
        headers=case.headers,
    )
    case.session_id = created.get_json()["id"]
    plan = case.client.post(
        f"/api/v1/sessions/{case.session_id}/generation-plan",
        json={"segments": [{"text": f"Sentence {index}"} for index in range(count)]},
        headers=case.headers,
    )
    assert plan.status_code == 201, plan.get_json()
    case.segment_ids = [item["id"] for item in page(case)["items"]]
    first = case._start()
    edit(case, 0, "Edited zero")
    second = case._start()
    edit(case, 1, "Edited one")
    with case.database.session() as session:
        ancestors = [session.get(GenerationRun, value["id"]).plan_revision_id
                     for value in (first, second)]
        final = page(case)["plan_revision_id"]
        assert final not in ancestors
        ownership = resolve_artifact_ownership(session)
        segments = {}
        for revision_id in (*ancestors, final):
            for row in session.scalars(select(GenerationSegment).where(
                GenerationSegment.plan_revision_id == revision_id
            )):
                segments[revision_id, row.ordinal] = row
        segment_ids = [row.id for row in segments.values()]
        # Start from revisions whose audio the reconcile has to discover.
        session.query(AudioTake).filter(
            AudioTake.generation_segment_id.in_(segment_ids)
        ).update({"parent_take_id": None}, synchronize_session=False)
        session.query(AudioTake).filter(
            AudioTake.generation_segment_id.in_(segment_ids)
        ).delete(synchronize_session=False)

        def artifact(name, ordinal):
            target = segments[final, ordinal]
            row = Artifact(
                workspace_id=ownership.workspace_id, project_id=ownership.project_id,
                kind="audio", relative_path=f"reconcile-{name}.wav",
                metadata_json={"synthesized_text": target.optimized_text or target.text},
            )
            session.add(row)
            session.flush()
            return row

        def take(revision_id, ordinal, row, status="completed", active=False, stamp=0):
            value = AudioTake(
                generation_segment_id=segments[revision_id, ordinal].id,
                artifact_id=row.id, status=status, is_active=active, revision=2,
                created_at=datetime(2026, 1, 1) + timedelta(minutes=stamp),
            )
            session.add(value)
            session.flush()
            return value

        old, mid = ancestors
        # 0: nothing to copy, the target already holds the artifact.
        held = artifact("held", 0)
        take(old, 0, held, stamp=1)
        take(final, 0, held, active=True, stamp=2)
        # 1: a late completed take on an edited segment (input differs: stale clone).
        take(old, 1, artifact("late-edited", 1), stamp=3)
        # 2: a late completed take that matches, target without takes.
        take(mid, 2, artifact("late-match", 2), stamp=4)
        # 3: one artifact under two ancestors, and a different one behind it.
        shared = artifact("shared", 3)
        take(old, 3, shared, stamp=5)
        take(mid, 3, shared, stamp=6)
        take(old, 3, artifact("second", 3), status="stale", stamp=7)
        # 4: removed target. 5: target with other source ids.
        if declined_targets:
            segments[final, 4].removed = True
            segments[final, 5].source_segment_ids_json = ["other"]
        take(mid, 4, artifact("removed", 4), stamp=8)
        take(mid, 5, artifact("sources", 5), stamp=9)
        # 6: stale ancestor take, target already has an active take.
        take(final, 6, artifact("active", 6), active=True, stamp=10)
        take(old, 6, artifact("stale-behind", 6), status="stale", stamp=11)
        # 7: stale ancestor take, target without an active take.
        take(final, 7, artifact("inactive", 7), stamp=12)
        take(old, 7, artifact("stale-open", 7), status="stale", stamp=13)
        # 8: target's active take is not completed; a matching late take replaces it.
        take(final, 8, artifact("not-done", 8), status="stale", active=True, stamp=14)
        take(mid, 8, artifact("replacement", 8), stamp=15)
        # 9: ancestor takes that are neither completed nor stale never copy.
        take(old, 9, artifact("failed", 9), status="failed", stamp=16)
        take(mid, 9, artifact("running", 9), status="running", stamp=17)
        # 10: an ancestor segment with no target at its ordinal never copies.
        orphan = GenerationSegment(plan_revision_id=old, ordinal=count, text="Orphan")
        session.add(orphan)
        orphan_audio = Artifact(
            workspace_id=ownership.workspace_id, project_id=ownership.project_id,
            kind="audio", relative_path="reconcile-orphan.wav",
            metadata_json={"synthesized_text": "Orphan"},
        )
        session.add(orphan_audio)
        session.flush()
        segments[old, count] = orphan
        take(old, count, orphan_audio, stamp=18)
    return final


def _reconcile_outcome(case, reconcile, revision_id, *, keep=False):
    outcome = {}
    try:
        with case.database.immediate_session() as session:
            outcome["changed"] = reconcile(session, revision_id)
            ordinals = {row.id: row.ordinal for row in session.scalars(
                select(GenerationSegment).where(
                    GenerationSegment.plan_revision_id == revision_id)
            )}
            outcome["takes"] = [
                (ordinals[value.generation_segment_id], value.artifact_id,
                 value.parent_take_id, value.kind, value.status, value.settings_hash,
                 value.duration_ms, value.is_active, value.revision, value.generation_run_id)
                for value in session.scalars(
                    select(AudioTake).where(AudioTake.generation_segment_id.in_(ordinals))
                    .order_by(text("audio_takes.rowid"))
                )
            ]
            outcome["segments"] = {
                ordinals[row.id]: (row.status, row.removed)
                for row in session.scalars(select(GenerationSegment).where(
                    GenerationSegment.plan_revision_id == revision_id))
            }
            if not keep:
                raise _Rollback
    except _Rollback:
        pass
    return outcome


@pytest.mark.usefixtures("host_execution_actor")
def test_reconcile_copies_the_same_takes_as_the_full_scan(case, monkeypatch):
    from pandrator.web import generation_edit_audio
    from pandrator.web.generation_edit_audio import inherit_edit_copy_audio

    revision_id = _build_edit_copy_with_two_ancestors(case)
    expected = _reconcile_outcome(case, _reference_inherit_edit_copy_audio, revision_id)
    actual = _reconcile_outcome(case, inherit_edit_copy_audio, revision_id)

    # The fixture covers copying and declining; a reference that copies nothing proves nothing.
    copied = [item for item in expected["takes"] if item[2] is not None]
    assert expected["changed"] == len(copied) == 7
    # Ordinal 3: the shared artifact once, plus the stale one behind it.
    assert sorted(item[0] for item in copied) == [1, 2, 3, 3, 6, 7, 8]
    assert any(item[7] for item in copied) and any(item[4] == "stale" for item in copied)
    assert actual == expected

    # Batches of two make every batched load span several batches, as a long plan does.
    monkeypatch.setattr(generation_edit_audio, "_BATCH", 2)
    assert _reconcile_outcome(case, inherit_edit_copy_audio, revision_id) == expected


@pytest.mark.usefixtures("host_execution_actor")
def test_reconcile_candidate_query_avoids_the_global_status_index(case):
    from pandrator.web.generation_edit_audio import inherit_edit_copy_audio

    revision_id = _build_edit_copy_with_two_ancestors(case)
    captured = []

    def capture_sql(_connection, _cursor, statement, parameters, _context, _many):
        # Only the candidate query compares `status || ''`; other SELECTs, such as the
        # ancestor walk or session setup, never match.
        if "AUDIO_TAKES.STATUS ||" in " ".join(statement.upper().split()):
            captured.append((statement, parameters))

    event.listen(case.database.engine, "before_cursor_execute", capture_sql)
    try:
        _reconcile_outcome(case, inherit_edit_copy_audio, revision_id)
    finally:
        event.remove(case.database.engine, "before_cursor_execute", capture_sql)

    (statement, parameters), = captured
    with case.database.engine.connect() as connection:
        plan = connection.exec_driver_sql(f"EXPLAIN QUERY PLAN {statement}", parameters).all()
    details = [str(row[3]).upper() for row in plan]
    # The status index holds every take of every project; the query must start from the
    # revision's segments instead. This holds on any SQLite version, with or without
    # statistics: comparing `status || ''` leaves no status index usable for the filter.
    # A full scan of the takes would cost the same, so it is rejected too.
    assert not any("IX_AUDIO_TAKES_STATUS" in detail
                   or "IDX_AUDIO_TAKES_ACTIVE_STATUS_SEGMENT_CREATED" in detail
                   or detail.startswith(("SCAN AUDIO_TAKES", "SCAN TABLE AUDIO_TAKES"))
                   for detail in details), details


@pytest.mark.usefixtures("host_execution_actor")
def test_reconcile_with_nothing_to_copy_hydrates_no_segment_or_take(case):
    from pandrator.web.generation_edit_audio import edit_copy_ancestors, inherit_edit_copy_audio

    revision_id = _build_edit_copy_with_two_ancestors(case, declined_targets=False)
    assert _reconcile_outcome(case, inherit_edit_copy_audio, revision_id, keep=True)["changed"] == 9

    with case.database.session() as session:
        ancestors = edit_copy_ancestors(session, session.get(GenerationPlanRevision, revision_id))
    assert len(ancestors) == 2
    loaded = []
    statements = []

    def count_load(target, _context):
        loaded.append(type(target).__name__)

    def capture_sql(_connection, _cursor, statement, _parameters, _context, _many):
        if statement.lstrip().upper().startswith("SELECT"):
            statements.append(" ".join(statement.split()))

    event.listen(GenerationSegment, "load", count_load)
    event.listen(AudioTake, "load", count_load)
    event.listen(case.database.engine, "before_cursor_execute", capture_sql)
    try:
        with case.database.immediate_session() as session:
            assert inherit_edit_copy_audio(session, revision_id) == 0
    finally:
        event.remove(case.database.engine, "before_cursor_execute", capture_sql)
        event.remove(AudioTake, "load", count_load)
        event.remove(GenerationSegment, "load", count_load)

    assert loaded == []
    # The revision, each ancestor and one candidate query.
    assert len(statements) <= 1 + len(ancestors) + 1, statements
