"""The workflow speaker plan guard refuses any cast revision that lost its plan.

Every path that synthesises from a revision passes ``freeze_speech_snapshot``,
so the workflow audio stage refuses a revision that lost its lineage's adopted
plan until the generation start acknowledges it once for that revision.
"""

import dataclasses
import json
import threading
from pathlib import Path
from unittest.mock import patch

import pytest
from sqlalchemy import func, select

from pandrator.web import models as m
from pandrator.web import performance_plans as plans
from tests.test_generation_cast_runtime import seed_cast
from tests.test_performance_plans import case as performance_case

case = performance_case

SETTINGS = {
    "service": "gemini",
    "model": "gemini-2.5-flash-tts",
    "voice": "Kore",
    "casting_enabled": True,
    "performance_enabled": False,
    "tts_batch_size": 4,
}


def _source(case, name, records, *, role="tts_optimized"):
    with case["services"]["database"].session() as session:
        storage_key = session.get(m.SessionRecord, case["session_id"]).storage_key
    path = Path(case["services"]["paths"].sessions) / storage_key / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(records), encoding="utf-8")
    return case["services"]["artifacts"].register(
        path, kind="json", role=role, session_id=case["session_id"]
    )


def _records(xml, *, second_marked):
    """A dialogue block with cast markup, a narration block, and a blank one."""
    narration = "Membership doubled soon afterwards."
    second = {"text": narration, "original_sentence": narration}
    if second_marked:
        second["speech_xml"] = f'<segment id="2">{narration}</segment>'
    return [
        {
            "text": "He said, “Bah!” Then left.",
            "original_sentence": "He said, “Bah!” Then left.",
            "speech_xml": xml,
        },
        second,
        {"text": "   ", "original_sentence": "   "},
    ]


def _generate(case, artifact, generated=None):
    """Run the live reviewable stage from the test's own source artifact."""
    from pandrator.web.workflow_generation_start import run_reviewable_generation

    services = case["services"]
    handlers = services["workflow_handlers"]
    recorded = [] if generated is None else generated
    context = dataclasses.replace(
        handlers._generation_start_context(),
        run_generation=lambda *args: recorded.append(args),
    )
    job = services["jobs"].enqueue(
        "workflow.continue",
        {"session_id": case["session_id"]},
        session_id=case["session_id"],
    )
    run_reviewable_generation(
        context,
        {
            "session_id": case["session_id"],
            "source_artifact_id": artifact.id,
            "settings": dict(SETTINGS),
        },
        lambda *_args: None,
        threading.Event(),
        resolved_snapshot={"tts": dict(SETTINGS)},
        job_id=job.id,
    )
    return recorded


def _active_revision(case):
    with case["services"]["database"].session() as session:
        plan = session.scalar(
            select(m.GenerationPlan).where(m.GenerationPlan.session_id == case["session_id"])
        )
        revision = session.get(m.GenerationPlanRevision, plan.active_revision_id)
        return revision.id, revision.parent_revision_id


def _drop_carried_plan(case, revision_id):
    """Leave the revision as one written before plans followed the blocks."""
    with case["services"]["database"].session() as session:
        for plan in session.scalars(
            select(m.PerformancePlan).where(m.PerformancePlan.plan_revision_id == revision_id)
        ):
            session.delete(plan)


def _count(case, model):
    with case["services"]["database"].session() as session:
        return session.scalar(select(func.count()).select_from(model))


def test_a_source_with_markup_on_every_spoken_block_still_needs_the_choice(case):
    adopted, xml = seed_cast(case)
    artifact = _source(case, "marked.json", _records(xml, second_marked=True))

    generated = []
    with pytest.raises(ValueError, match="no adopted speaker plan"):
        _generate(case, artifact, generated)
    revision_id, parent_id = _active_revision(case)
    # The run's revision descends from the adopted one and has no plan of its
    # own; its own cast markup is no longer enough.
    assert parent_id == adopted["plan_revision_id"]
    with case["services"]["database"].session() as session:
        assert plans.missing_speaker_plan(session, revision_id, SETTINGS) is not None
    assert generated == []
    assert _count(case, m.GenerationRun) == 0


def test_a_source_stored_from_new_text_is_refused_with_the_one_message(case):
    adopted, xml = seed_cast(case)
    artifact = _source(case, "mixed.json", _records(xml, second_marked=False))

    generated = []
    with pytest.raises(ValueError) as refused:
        _generate(case, artifact, generated)
    message = str(refused.value)
    revision_id, parent_id = _active_revision(case)
    assert "no adopted speaker plan" in message
    assert adopted["id"] in message
    assert "(allow_without_speaker_plan)" in message
    assert "that choice is kept for this revision and the revisions edited from it" in message
    # One rule: the message no longer names blocks that would be narrated.
    assert "spoken block" not in message
    assert _count(case, m.AudioTake) == 0
    # The refused run never reached synthesis.
    assert generated == []
    assert _count(case, m.GenerationRun) == 0
    # The source's revision was stored before the stage could check it: it is
    # stored and active, beside the adopted one; a retry reuses it.
    assert parent_id == adopted["plan_revision_id"]
    revisions = _count(case, m.GenerationPlanRevision)
    # A retry from the same source is refused again.
    with pytest.raises(ValueError, match="no adopted speaker plan"):
        _generate(case, artifact, generated)
    assert _count(case, m.GenerationPlanRevision) == revisions
    assert _active_revision(case)[0] == revision_id


def test_the_reviewable_stage_refuses_a_split_of_the_adopted_revision(case):
    from pandrator.web.workflow_generation_start import run_reviewable_generation

    adopted, _xml = seed_cast(case)
    services = case["services"]
    split = services["generation"].revise_topology(
        case["session_id"],
        adopted["plan_revision_id"],
        {"action": "split", "segment_id": case["segment_ids"][1], "cursor": 3, "text_layer": "display"},
    )
    split_id = split["plan_revision_id"]
    _drop_carried_plan(case, split_id)
    handlers = services["workflow_handlers"]
    artifact = _source(case, "prepared.json", [{"text": "Unused."}], role="prepared_text")
    generated = []
    context = dataclasses.replace(
        handlers._generation_start_context(),
        run_generation=lambda *args: generated.append(args),
    )
    job = services["jobs"].enqueue(
        "workflow.continue", {"session_id": case["session_id"]}, session_id=case["session_id"]
    )

    def run(settings):
        return run_reviewable_generation(
            context,
            {"session_id": case["session_id"], "source_artifact_id": artifact.id, "settings": {}},
            lambda value, detail=None: None,
            threading.Event(),
            resolved_snapshot={"tts": dict(settings)},
            settings_hash="caller-hash",
            job_id=job.id,
        )

    with pytest.raises(ValueError) as refused:
        run(SETTINGS)
    message = str(refused.value)
    assert "no adopted speaker plan" in message
    assert adopted["id"] in message
    assert "(allow_without_speaker_plan)" in message
    assert generated == []
    assert _count(case, m.GenerationRun) == 0

    # Control: with casting off the guard has nothing to protect.
    class PastTheGuard(Exception):
        pass

    with patch(
        "pandrator.web.speech_plan_workspace.freeze_speech_snapshot", side_effect=PastTheGuard()
    ):
        with pytest.raises(PastTheGuard):
            run({**SETTINGS, "casting_enabled": False})


def test_the_stage_passes_once_the_revision_is_acknowledged(case):
    from pandrator.web.workflow_generation_start import run_reviewable_generation

    adopted, _xml = seed_cast(case)
    services = case["services"]
    split = services["generation"].revise_topology(
        case["session_id"],
        adopted["plan_revision_id"],
        {"action": "split", "segment_id": case["segment_ids"][1], "cursor": 3, "text_layer": "display"},
    )
    split_id = split["plan_revision_id"]
    _drop_carried_plan(case, split_id)
    handlers = services["workflow_handlers"]
    artifact = _source(case, "prepared.json", [{"text": "Unused."}], role="prepared_text")
    generated = []
    context = dataclasses.replace(
        handlers._generation_start_context(),
        run_generation=lambda *args: generated.append(args),
    )
    job = services["jobs"].enqueue(
        "workflow.continue", {"session_id": case["session_id"]}, session_id=case["session_id"]
    )

    def run():
        return run_reviewable_generation(
            context,
            {"session_id": case["session_id"], "source_artifact_id": artifact.id, "settings": {}},
            lambda value, detail=None: None,
            threading.Event(),
            resolved_snapshot={"tts": dict(SETTINGS)},
            settings_hash="caller-hash",
            job_id=job.id,
        )

    with pytest.raises(ValueError) as refused:
        run()
    assert "no adopted speaker plan" in str(refused.value)
    assert '"Generate without the speaker plan" (allow_without_speaker_plan)' in str(
        refused.value
    )
    with services["database"].session() as session:
        plans.waive_speaker_plan(session, split_id, adopted["id"])
    # The same stage now reaches the run.
    run()
    assert len(generated) == 1
