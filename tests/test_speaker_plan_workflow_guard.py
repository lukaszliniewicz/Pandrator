"""The workflow speaker plan guard reads each block's own markup on real lineages.

The generation start refuses any revision that lost its lineage's adopted plan.
The workflow audio stage can run from a source that carries its own speech
markup, so it refuses only when a spoken block has none, nor a block voice or a
speaker label, and names those blocks. A direct dubbing job stores a new
revision on every run, so it is not checked, as upstream runs it.
"""

import dataclasses
import json
import threading
from pathlib import Path
from unittest.mock import patch

import pytest
from pydub import AudioSegment
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


def _generate(case, artifact, synthesize):
    handlers = case["services"]["workflow_handlers"]
    with patch.object(handlers.tts_providers, "synthesize", side_effect=synthesize):
        return handlers.generate_audiobook_audio(
            {
                "session_id": case["session_id"],
                "source_artifact_id": artifact.id,
                "settings": dict(SETTINGS),
            },
            lambda *_args: None,
            threading.Event(),
        )


def _active_revision(case):
    with case["services"]["database"].session() as session:
        plan = session.scalar(
            select(m.GenerationPlan).where(m.GenerationPlan.session_id == case["session_id"])
        )
        revision = session.get(m.GenerationPlanRevision, plan.active_revision_id)
        return revision.id, revision.parent_revision_id


def _segment_ids(case, revision_id):
    with case["services"]["database"].session() as session:
        return list(
            session.scalars(
                select(m.GenerationSegment.id)
                .where(m.GenerationSegment.plan_revision_id == revision_id)
                .order_by(m.GenerationSegment.ordinal)
            )
        )


def _drop_speaker_labels(case, revision_id):
    """The fixture labels every block with a source speaker; plain audiobook text has none."""
    with case["services"]["database"].session() as session:
        for segment in session.scalars(
            select(m.GenerationSegment).where(m.GenerationSegment.plan_revision_id == revision_id)
        ):
            segment.speaker = None


def _count(case, model):
    with case["services"]["database"].session() as session:
        return session.scalar(select(func.count()).select_from(model))


def test_a_source_with_markup_on_every_spoken_block_runs_from_it(case):
    adopted, xml = seed_cast(case)
    artifact = _source(case, "marked.json", _records(xml, second_marked=True))
    requests = []

    def synthesize(text, settings, **_kwargs):
        requests.append((text, settings["voice"]))
        return AudioSegment.silent(duration=20)

    assert _generate(case, artifact, synthesize)["artifact_id"]
    # The source's own cast markup voiced the dialogue.
    assert [voice for _text, voice in requests] == ["Kore", "Puck", "Kore", "Kore"]
    revision_id, parent_id = _active_revision(case)
    # The run's revision descends from the adopted one and has no plan of its
    # own, so the lineage rule applied and the per-block rule let it through.
    assert parent_id == adopted["plan_revision_id"]
    with case["services"]["database"].session() as session:
        assert plans.missing_speaker_plan(session, revision_id, SETTINGS) is not None
        assert plans.blocks_without_own_speech(session, revision_id) == []
        # The generation start keeps its broader rule on the same revision.
        with pytest.raises(ValueError, match="start again with allow_without_speaker_plan"):
            plans.refuse_missing_speaker_plan(session, revision_id, SETTINGS)


def test_a_mixed_source_is_refused_naming_its_blocks_without_markup(case):
    adopted, xml = seed_cast(case)
    artifact = _source(case, "mixed.json", _records(xml, second_marked=False))

    def synthesize(*_args, **_kwargs):
        raise AssertionError("a refused run must not synthesise")

    with pytest.raises(ValueError) as refused:
        _generate(case, artifact, synthesize)
    message = str(refused.value)
    revision_id, parent_id = _active_revision(case)
    # The job drops the blank record before storing the source.
    first, second = _segment_ids(case, revision_id)
    assert "1 spoken block has no speech markup, block voice or speaker label of its own" in message
    assert f"block 2 ({second})" in message
    # The marked dialogue block is not named.
    assert first not in message
    assert adopted["id"] in message
    assert '"Generate without the speaker plan" (allow_without_speaker_plan)' in message
    # This revision came from new source text, so a copy of the plan starts empty.
    assert (
        "Copy it to this revision and adopt it (a revision stored from new source text "
        "keeps none of its entries, so the copy needs analysis and review first), or "
    ) in message
    assert _count(case, m.AudioTake) == 0
    # The source's revision was stored before the stage could check it: it is
    # stored and active, beside the adopted one; a retry reuses it.
    assert parent_id == adopted["plan_revision_id"]
    revisions = _count(case, m.GenerationPlanRevision)
    # A retry from the same source is refused again.
    with pytest.raises(ValueError, match="1 spoken block has no speech markup"):
        _generate(case, artifact, synthesize)
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
    _drop_speaker_labels(case, split_id)
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
    # The dialogue block's cast lived only in the plan, so it has no markup;
    # the split's two halves start from the plan's markup and are not named.
    dialogue, left, right, last = _segment_ids(case, split_id)
    assert "2 spoken blocks have no speech markup, block voice or speaker label of their own" in message
    assert f"block 1 ({dialogue})" in message and f"block 4 ({last})" in message
    assert left not in message and right not in message
    assert adopted["id"] in message
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


def test_removed_and_blank_blocks_are_not_spoken(case):
    adopted, _xml = seed_cast(case)
    services = case["services"]
    split = services["generation"].revise_topology(
        case["session_id"],
        adopted["plan_revision_id"],
        {"action": "split", "segment_id": case["segment_ids"][1], "cursor": 3, "text_layer": "display"},
    )
    split_id = split["plan_revision_id"]
    _drop_speaker_labels(case, split_id)
    dialogue, _left, _right, last = _segment_ids(case, split_id)
    with services["database"].session() as session:
        assert plans.blocks_without_own_speech(session, split_id) == [(0, dialogue), (3, last)]
        removed = session.get(m.GenerationSegment, dialogue)
        removed.removed = True
        blank = session.get(m.GenerationSegment, last)
        blank.text, blank.optimized_text = "  ", None
    with services["database"].session() as session:
        # Nothing is left to voice as narration, so the per-block rule lets it run.
        assert plans.blocks_without_own_speech(session, split_id) == []
        plans.refuse_missing_speaker_plan(session, split_id, SETTINGS, from_generation_start=False)
        # The generation start keeps refusing on the lineage alone.
        with pytest.raises(ValueError, match="start again with allow_without_speaker_plan"):
            plans.refuse_missing_speaker_plan(session, split_id, SETTINGS)


def test_a_block_voice_or_speaker_label_is_its_own_speech(case):
    adopted, _xml = seed_cast(case)
    services = case["services"]
    split = services["generation"].revise_topology(
        case["session_id"],
        adopted["plan_revision_id"],
        {"action": "split", "segment_id": case["segment_ids"][1], "cursor": 3, "text_layer": "display"},
    )
    split_id = split["plan_revision_id"]
    _drop_speaker_labels(case, split_id)
    dialogue, _left, _right, last = _segment_ids(case, split_id)
    with services["database"].session() as session:
        assert plans.blocks_without_own_speech(session, split_id) == [(0, dialogue), (3, last)]
    with services["database"].session() as session:
        # A diarised speaker label is cast through source_speakers, and a block
        # voice applies to the whole block: neither falls back to the narrator.
        session.get(m.GenerationSegment, dialogue).speaker = "SPEAKER_01"
        session.get(m.GenerationSegment, last).voice = "Puck"
    with services["database"].session() as session:
        assert plans.blocks_without_own_speech(session, split_id) == []
        plans.refuse_missing_speaker_plan(session, split_id, SETTINGS, from_generation_start=False)
        # The generation start keeps refusing on the lineage alone.
        with pytest.raises(ValueError, match="start again with allow_without_speaker_plan"):
            plans.refuse_missing_speaker_plan(session, split_id, SETTINGS)


def test_direct_dubbing_runs_unguarded_and_keeps_upstream_performance_text(case):
    from pandrator.web.workflow_generation_automatic import generate_audio

    adopted, _xml = seed_cast(case)
    current = adopted["plan_revision_id"]
    revisions = _count(case, m.GenerationPlanRevision)
    # Unlabelled records that repeat the plan's marked-up dialogue.
    artifact = _source(
        case,
        "dubbing.json",
        [{"text": "He said, “Bah!” Then left."}],
        role="speech_blocks",
    )
    handlers = case["services"]["workflow_handlers"]
    _artifact, path = handlers._resolve_input(artifact.id)
    frozen = []

    class PastTheFreeze(Exception):
        pass

    def freeze(_session, revision_id, _snapshot):
        frozen.append(revision_id)
        raise PastTheFreeze()

    with patch("pandrator.web.speech_plan_workspace.freeze_speech_snapshot", side_effect=freeze):
        with pytest.raises(PastTheFreeze):
            generate_audio(
                handlers._automatic_generation_context(),
                case["session_id"],
                artifact,
                path,
                dict(SETTINGS),
                lambda *_args, **_kwargs: None,
                threading.Event(),
                role="dubbing_audio",
            )
    stored = frozen[0]
    assert _count(case, m.GenerationPlanRevision) == revisions + 1
    assert _active_revision(case) == (stored, current)
    with case["services"]["database"].session() as session:
        # The stage rule would refuse this revision; the job is not subject to it.
        with pytest.raises(ValueError, match="no adopted speaker plan"):
            plans.refuse_missing_speaker_plan(
                session, stored, SETTINGS, from_generation_start=False
            )
        # Performance on: upstream's text, since a copied plan cannot help a job
        # that stores another revision on its next run.
        assert plans.no_adopted_performance_plan_message(session, stored) == (
            "Performance is enabled, but this speech plan has no adopted performance plan. "
            "Adopt one or disable performance."
        )
    # An edit of the stored revision is no longer the job's own: it names the plan.
    segment_id = _segment_ids(case, stored)[0]
    split = case["services"]["generation"].revise_topology(
        case["session_id"],
        stored,
        {"action": "split", "segment_id": segment_id, "cursor": 3, "text_layer": "display"},
    )
    with case["services"]["database"].session() as session:
        message = plans.no_adopted_performance_plan_message(session, split["plan_revision_id"])
    assert adopted["id"] in message
    assert "Copy it to this revision" in message
