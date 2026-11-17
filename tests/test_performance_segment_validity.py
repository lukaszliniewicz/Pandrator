"""An edit makes only the edited segment's speaker plan entry stale.

An adopted speaker plan is judged per segment against each unit's stored
``text_hash``: unchanged segments keep their entries, an edited segment keeps
its markup while the change lies inside one spoken string of it, and only the
rest need a new review. Work is counted, never timed.
"""

from __future__ import annotations

import uuid
from contextlib import contextmanager
from copy import deepcopy

import pytest
from sqlalchemy import select

from pandrator.logic.speech_markup import parse_speech_markup
from pandrator.logic.speech_performance import content_hash, validate_annotation
from pandrator.web import models as m
from pandrator.web import performance_plans as plans
from pandrator.web import performance_validity as validity
from pandrator.web.generation_audio_identity import IDENTITY_KEY, AudioIdentityContext
from pandrator.web.generation_performance_snapshot import refuse_review_segments
from pandrator.web.performance_validity import REVIEW_MESSAGE, fit_entry
from pandrator.web.projects.artifact_identity import resolve_artifact_ownership
from pandrator.web.speech_annotation_records import normalized_record, record_markup
from pandrator.web.speech_plan_workspace import freeze_speech_snapshot
from tests.test_generation_cast_runtime import seed_cast
from tests.test_performance_plans import adopt, create, edit, get
from tests.test_performance_plans import case as case  # noqa: F401

CHARACTERS = [{"id": "c-scrooge", "display_name": "Scrooge", "voice_category": "male"}]
DIALOGUE_TEXT = "He said, “Bah!” Then left."
DIALOGUE_XML = (
    '<segment id="s">He said, <dialogue><speaker ref="c-scrooge"><em>cross</em>'
    "“Bah!”</speaker></dialogue> Then left.</segment>"
)


def xml_record(text: str, xml: str, *, locked: bool = False, reason: str = "") -> dict:
    return normalized_record(
        text, "s", {"speech_xml": xml, "locked": locked, "reason": reason}, CHARACTERS
    )


def fit(old: str, record: dict, new: str) -> dict | None:
    return fit_entry({"id": "s", "spoken_text": old}, record, new, "s", CHARACTERS)


def parsed(record: dict, text: str):
    xml = record_markup(record)
    assert xml is not None
    return parse_speech_markup(
        xml, expected_segment_id="s", expected_text=text, characters=CHARACTERS
    )


# fit_entry units --------------------------------------------------------------


def test_plain_entry_fits_any_edit():
    record = xml_record("Hello there.", '<segment id="s" boundary_after="paragraph">Hello there.</segment>')
    for new in ("Hello here.", "Oh, hello there.", "Hello there. Again.", "Bye."):
        fitted = fit("Hello there.", record, new)
        assert fitted is not None, new
        assert parsed(fitted, new).boundary_after == "paragraph"
    narrated = xml_record("Hello there.", '<segment id="s"><narrator>Hello there.</narrator></segment>')
    fitted = fit("Hello there.", narrated, "Hello there. Again.")
    assert fitted is not None
    assert all(span.narrator for span in parsed(fitted, "Hello there. Again.").spans)


def test_change_inside_a_span_keeps_the_dialogue_and_its_controls():
    record = xml_record(DIALOGUE_TEXT, DIALOGUE_XML, locked=True, reason="Kept by hand.")
    new = "He said, “Pah!” Then left."
    fitted = fit(DIALOGUE_TEXT, record, new)
    assert fitted is not None
    spans = parsed(fitted, new).spans
    assert [(span.text, span.dialogue, span.speaker_id) for span in spans] == [
        ("He said, ", False, None),
        ("“Pah!”", True, "c-scrooge"),
        (" Then left.", False, None),
    ]
    assert spans[1].delivery["emotion"] == "cross"
    assert fitted["locked"] is True and fitted["reason"] == "Kept by hand."
    # A change in the narration after the dialogue fits too.
    assert fit(DIALOGUE_TEXT, record, "He said, “Bah!” Then he left.") is not None


def test_change_crossing_a_span_boundary_does_not_fit():
    record = xml_record(DIALOGUE_TEXT, DIALOGUE_XML)
    assert fit(DIALOGUE_TEXT, record, "He said, “Bah,” then left.") is None
    assert fit(DIALOGUE_TEXT, record, "He left.") is None


def test_insertion_at_a_shared_boundary_is_ambiguous():
    record = xml_record(DIALOGUE_TEXT, DIALOGUE_XML)
    # "!" lands exactly where the dialogue ends and the narration begins.
    assert fit(DIALOGUE_TEXT, record, "He said, “Bah!”! Then left.") is None
    event = xml_record("Hello there.", '<segment id="s">Hello<event kind="laugh"/> there.</segment>')
    assert fit("Hello there.", event, "Hello, there.") is None
    assert fit("Hello there.", event, "Hello there!") is not None


def test_narration_at_the_edge_of_a_dialogue_does_not_fit():
    quote = "“Bah!”"
    record = xml_record(
        quote,
        '<segment id="s"><dialogue><speaker ref="c-scrooge">“Bah!”</speaker></dialogue></segment>',
    )
    # An attribution after a closing quote, or a lead-in before an opening one,
    # could be narration, so neither is rebased into the speaker's line.
    assert fit(quote, record, "“Bah!” he said.") is None
    assert fit(quote, record, "He said, “Bah!”") is None
    fitted = fit(quote, record, "“Pah!”")
    assert fitted is not None and parsed(fitted, "“Pah!”").spans[0].dialogue


def test_text_at_the_edge_of_a_voiced_span_does_not_fit():
    text = "He said hello"
    record = xml_record(text, '<segment id="s">He said <span voice="Puck">hello</span></segment>')
    # Appended text could be narration after the voiced words, so it is not
    # rebased into the span's voice; a change inside the span still fits.
    assert fit(text, record, "He said hello there.") is None
    fitted = fit(text, record, "He said hallo")
    assert fitted is not None
    assert [(span.text, span.voice) for span in parsed(fitted, "He said hallo").spans] == [
        ("He said ", None),
        ("hallo", "Puck"),
    ]


def test_text_at_the_edge_of_a_narrator_with_its_own_voice_does_not_fit():
    text = "Then I remember."
    record = xml_record(text, '<segment id="s">Then <narrator voice="Puck">I remember.</narrator></segment>')
    # A narrator with its own voice is voiced like a span with one.
    assert fit(text, record, "Then I remember. He sighed.") is None
    fitted = fit(text, record, "Then I recall.")
    assert fitted is not None
    assert [(span.text, span.voice) for span in parsed(fitted, "Then I recall.").spans] == [
        ("Then ", None),
        ("I recall.", "Puck"),
    ]


def test_a_change_that_repeats_the_text_at_an_edge_does_not_slip_across_it():
    old = "He said “Go.”"
    record = xml_record(
        old,
        '<segment id="s">He said <dialogue><speaker ref="c-scrooge">“Go.”</speaker></dialogue></segment>',
    )
    # Matched from the start, ',” he said “Go' would sit inside the speaker's
    # line; matched from the end it crosses the opening quote's edge.
    assert fit(old, record, "He said “Go,” he said “Go.”") is None
    bare = "He said Go."
    bare_record = xml_record(
        bare,
        '<segment id="s">He said <dialogue><speaker ref="c-scrooge">Go.</speaker></dialogue></segment>',
    )
    assert fit(bare, bare_record, "He said Go, he said Go.") is None
    # A change that stays inside the line wherever it sits still fits.
    fitted = fit(old, record, "He said “Goo.”")
    assert fitted is not None
    assert [span.text for span in parsed(fitted, "He said “Goo.”").spans if span.dialogue] == [
        "“Goo.”"
    ]


def test_narration_that_gains_or_loses_a_quotation_mark_needs_review():
    record = xml_record(DIALOGUE_TEXT, DIALOGUE_XML)
    # Narration beside a spoken line that gains a quotation mark may now hold
    # dialogue the narrator would read.
    assert fit(DIALOGUE_TEXT, record, "He said, “Bah!” Then “Go,” he left.") is None
    framed_text = "“Bah!” he said."
    framed = xml_record(
        framed_text,
        '<segment id="s">“<dialogue><speaker ref="c-scrooge">Bah!</speaker></dialogue>” he said.</segment>',
    )
    assert fit(framed_text, framed, "Bah!” he said.") is None
    # Other narration edits still fit, an apostrophe included; an edited
    # attribution keeps the speaker it had.
    assert fit(DIALOGUE_TEXT, record, "He said, “Bah!” Then didn’t leave.") is not None
    renamed = fit(DIALOGUE_TEXT, record, "She said, “Bah!” Then left.")
    assert renamed is not None
    assert [span.speaker_id for span in parsed(renamed, "She said, “Bah!” Then left.").spans] == [
        None, "c-scrooge", None,
    ]
    # A block without a spoken line is not judged by quotation marks.
    plain = xml_record("He said hi.", '<segment id="s">He said hi.</segment>')
    assert fit("He said hi.", plain, "He said “hi.”") is not None


def test_a_spoken_line_that_gains_or_loses_a_quotation_mark_needs_review():
    old = "He said “Go now.”"
    record = xml_record(
        old,
        '<segment id="s">He said <dialogue><speaker ref="c-scrooge">“Go now.”</speaker></dialogue></segment>',
    )
    # Split around an attribution, the line would make the speaker voice it.
    assert fit(old, record, "He said “Go,” he said, “now.”") is None
    # Without a quotation mark nothing tells an attribution from speech: the
    # speaker keeps all of it, as the docs say.
    bare = "He said Go now."
    bare_record = xml_record(
        bare,
        '<segment id="s">He said <dialogue><speaker ref="c-scrooge">Go now.</speaker></dialogue></segment>',
    )
    assert fit(bare, bare_record, "He said Go, he said, now.") is not None
    # An apostrophe inside a word still fits.
    assert fit(old, record, "He said “Go now, it’s late.”") is not None


def test_single_quotation_marks_count_unless_inside_a_word():
    record = xml_record(DIALOGUE_TEXT, DIALOGUE_XML)
    assert fit(DIALOGUE_TEXT, record, "He said, “Bah!” Then 'Go,' he left.") is None
    assert fit(DIALOGUE_TEXT, record, "He said, “Bah!” Then ‘Go’ he left.") is None
    assert fit(DIALOGUE_TEXT, record, "He said, “Bah!” Then ＂Go＂ he left.") is None
    assert fit(DIALOGUE_TEXT, record, "He said, “Bah!” Then 「Go」 he left.") is None
    framed_text = "‘Bah!’ he said."
    framed = xml_record(
        framed_text,
        '<segment id="s">‘<dialogue><speaker ref="c-scrooge">Bah!</speaker></dialogue>’ he said.</segment>',
    )
    # The closing mark is the apostrophe character, but no letter follows it.
    assert fit(framed_text, framed, "‘Bah! he said.") is None
    assert fit(DIALOGUE_TEXT, record, "He said, “Bah!” Then didn't leave.") is not None
    assert fit(framed_text, framed, "‘Bah!’ he hadn’t said.") is not None


def test_an_insertion_beside_an_event_or_control_at_a_block_edge_does_not_fit():
    old = "Hello there."
    trailing = xml_record(old, '<segment id="s">Hello there.<event kind="laugh"/></segment>')
    # Text added after the last words could come before or after the laugh.
    assert fit(old, trailing, "Hello there. Again.") is None
    leading = xml_record(old, '<segment id="s"><event kind="laugh"/>Hello there.</segment>')
    assert fit(old, leading, "Oh. Hello there.") is None
    control = xml_record(old, '<segment id="s"><em>calm</em>Hello there.</segment>')
    assert fit(old, control, "Oh. Hello there.") is None
    # A change inside the words beside it still fits, and keeps the event.
    fitted = fit(old, trailing, "Hello here.")
    assert fitted is not None
    assert record_markup(fitted) == '<segment id="s">Hello here.<event kind="laugh"/></segment>'
    assert fit(old, leading, "Hello here.") is not None


def test_a_carriage_return_survives_the_rebase():
    record = xml_record("Helo\rthere.", '<segment id="s">Helo&#13;there.</segment>')
    fitted = fit("Helo\rthere.", record, "Hello\rthere.")
    assert fitted is not None
    assert "&#13;" in record_markup(fitted)
    assert parsed(fitted, "Hello\rthere.").transcript == "Hello\rthere."


def test_rebased_markup_that_fails_to_parse_does_not_fit():
    record = xml_record("Hi there.", '<segment id="s">Hi there.</segment>')
    # A character XML 1.0 cannot represent never parses.
    assert fit("Hi there.", record, "Hi\x01there.") is None


def test_legacy_entry_fits_while_it_validates():
    old = "Yet this defeat would be temporary."
    record = validate_annotation(old, {
        "decision": "steer",
        "spans": [{"anchor": {"quote": "defeat"}, "delivery": {"instruction": "Weigh it"}}],
    })
    fitted = fit(old, record, "Yet this defeat would be brief.")
    assert fitted is not None and fitted["spans"][0]["anchor"]["quote"] == "defeat"
    assert fit(old, record, "Yet this loss would be brief.") is None
    assert fit(old, None, "Anything.") is None


def legacy_record(text: str, quote: str, occurrence: int | None = None) -> dict:
    anchor = {"quote": quote} if occurrence is None else {"quote": quote, "occurrence": occurrence}
    return validate_annotation(text, {
        "decision": "steer",
        "spans": [{"anchor": anchor, "delivery": {"instruction": "Weigh it"}}],
    })


def test_legacy_anchor_moved_by_an_earlier_copy_of_its_quote_does_not_fit():
    old = "He paused. Then the defeat came, and the defeat stayed."
    record = legacy_record(old, "defeat", 2)
    # A new earlier copy makes occurrence 2 the first "defeat" of the old text.
    assert fit(old, record, "A defeat. He paused. Then the defeat came, and the defeat stayed.") is None
    # Removing an earlier copy moves it too.
    assert fit(old, record, "He paused. Then it came, and the defeat stayed.") is None
    # An event's anchor is held to the same rule.
    event = validate_annotation(old, {
        "decision": "steer",
        "events": [{"kind": "sigh", "anchor": {"quote": "defeat", "occurrence": 2}}],
    })
    assert fit(old, event, "A defeat. He paused. Then the defeat came, and the defeat stayed.") is None
    assert fit(old, event, "He paused long. Then the defeat came, and the defeat stayed.") is not None
    # A copy written right beside the anchored words can sit before or after
    # them; placed before, the anchor moves, so the entry does not fit.
    beside = "Then the defeat stayed."
    assert fit(beside, legacy_record(beside, "defeat", 1), "Then the defeat defeat stayed.") is None


def test_legacy_anchor_fits_an_edit_before_or_after_it_without_a_new_copy():
    old = "Yet this defeat would be temporary."
    record = legacy_record(old, "defeat")
    assert fit(old, record, "Yet this defeat would be brief.") is not None
    assert fit(old, record, "And yet this defeat would be temporary.") is not None
    repeated = "The defeat came, and the defeat stayed."
    second = legacy_record(repeated, "defeat", 2)
    assert fit(repeated, second, "The defeat came, and the defeat stayed long.") is not None
    assert fit(repeated, second, "The first defeat came, and the defeat stayed.") is not None


# Plan reads and freezes -------------------------------------------------------


def set_text(case, index: int, text: str, *, removed: bool | None = None) -> None:
    with case["services"]["database"].session() as session:
        segment = session.get(m.GenerationSegment, case["segment_ids"][index])
        segment.text = text
        if removed is not None:
            segment.removed = removed


def freeze(case) -> dict:
    snapshot: dict = {}
    with case["services"]["database"].session() as session:
        plans.freeze_performance_snapshot(session, case["revision_id"], snapshot)
    return snapshot["performance_snapshot"]


def test_unchanged_plan_freezes_and_reads_as_before(case):
    adopted = adopt(case, edit(case, create(case)))
    assert adopted["stale"] is False
    assert (adopted["changed_segment_count"], adopted["review_segment_count"],
            adopted["review_segments"]) == (0, 0, [])
    frozen = freeze(case)
    assert "review_segment_ids" not in frozen
    assert list(frozen["annotations"]) == case["segment_ids"]


def test_fitting_edit_is_changed_not_review_and_the_plan_stays_immutable(case):
    adopted = adopt(case, edit(case, create(case)))
    with case["services"]["database"].session() as session:
        before = deepcopy(session.get(m.PerformancePlan, adopted["id"]).units_json)
    set_text(case, 1, "Yet this defeat would be brief.")
    read = get(case, adopted)
    assert read["stale"] is True
    assert (read["changed_segment_count"], read["review_segment_count"],
            read["review_segments"]) == (1, 0, [])
    frozen = freeze(case)
    assert frozen["review_segment_ids"] == []
    entry = frozen["annotations"][case["segment_ids"][1]]
    assert entry["text_hash"] == content_hash("Yet this defeat would be brief.")
    assert entry["annotation"]["delivery"]["instruction"] == "Mark a restrained contrast"
    with case["services"]["database"].session() as session:
        assert session.get(m.PerformancePlan, adopted["id"]).units_json == before


def test_removed_blank_and_unplanned_segments(case):
    set_text(case, 2, "   ")  # blank: never planned
    adopted = adopt(case, create(case))
    set_text(case, 2, "Membership doubled soon afterwards.")  # spoken, with no unit
    set_text(case, 0, "Rejected.", removed=True)  # removed: changed
    read = get(case, adopted)
    # The unplanned segment's text changed too, so it is counted as changed.
    assert (read["changed_segment_count"], read["review_segment_count"]) == (2, 1)
    assert read["review_segments"] == [{"ordinal": 2, "id": case["segment_ids"][2]}]
    frozen = freeze(case)
    assert set(frozen["annotations"]) == {case["segment_ids"][1]}
    assert frozen["review_segment_ids"] == [case["segment_ids"][2]]
    set_text(case, 1, "   ")  # a planned segment blanked: changed, not frozen
    read = get(case, adopted)
    assert (read["changed_segment_count"], read["review_segment_count"]) == (3, 1)
    assert freeze(case)["annotations"] == {}


def test_plan_not_on_the_active_revision_is_not_judged(case):
    adopted = adopt(case, create(case))
    with case["services"]["database"].session() as session:
        selected = session.scalar(
            select(m.GenerationPlan).where(m.GenerationPlan.session_id == case["session_id"])
        )
        assert selected is not None
        selected.active_revision_id = None
    read = get(case, adopted)
    assert read["stale"] is True
    assert (read["changed_segment_count"], read["review_segment_count"],
            read["review_segments"]) == (None, None, None)


def test_a_draft_plan_is_not_judged(case):
    adopt(case, create(case))
    draft = create(case)
    set_text(case, 1, "Yet this defeat would be brief.")
    read = get(case, draft)
    assert read["status"] == "draft" and read["stale"] is True
    assert (read["changed_segment_count"], read["review_segment_count"],
            read["review_segments"]) == (None, None, None)


def test_review_lists_are_bounded(case, monkeypatch):
    adopted = adopt(case, create(case))
    set_text(case, 1, "Yet this defeat would be brief.")
    review = tuple((ordinal, f"seg-{ordinal}") for ordinal in range(25))
    monkeypatch.setattr(
        plans, "plan_validity",
        lambda *_args, **_kwargs: validity.PlanValidity({}, (), review),
    )
    read = get(case, adopted)
    assert read["review_segment_count"] == 25
    assert read["review_segments"] == [
        {"ordinal": ordinal, "id": f"seg-{ordinal}"} for ordinal in range(20)
    ]


def test_run_refusal_names_five_blocks_from_one(case):
    seeded = case["services"]["generation"].create_plan(
        case["session_id"], source_revision_id=None, settings={},
        segments=[{"text": f"Line {index}.", "speaker": "A"} for index in range(7)],
    )
    with case["services"]["database"].session() as session:
        ids = list(session.scalars(
            select(m.GenerationSegment.id)
            .where(m.GenerationSegment.plan_revision_id == seeded["active_revision_id"])
            .order_by(m.GenerationSegment.ordinal)
        ))
        snapshot = {"performance_snapshot": {"review_segment_ids": list(reversed(ids))}}
        with pytest.raises(ValueError) as refused:
            refuse_review_segments(session, snapshot, None)
        # A scope without any review block starts.
        refuse_review_segments(session, snapshot, [])
    message = str(refused.value)
    assert message.startswith(
        "7 blocks changed after the speaker plan was adopted and no longer fit their "
        "speaker markup: "
    )
    assert message.endswith(
        "Review a new speaker plan; until then, only a generation run that leaves "
        "these blocks out can start."
    )
    # A single block reads in the singular.
    with case["services"]["database"].session() as session:
        with pytest.raises(ValueError) as single:
            refuse_review_segments(
                session, {"performance_snapshot": {"review_segment_ids": [ids[0]]}}, None
            )
    assert str(single.value) == (
        "1 block changed after the speaker plan was adopted and no longer fits its "
        f"speaker markup: block 1 ({ids[0]}). Review a new speaker plan; until then, "
        "only a generation run that leaves this block out can start."
    )
    assert ", ".join(f"block {index + 1} ({ids[index]})" for index in range(5)) in message
    assert ids[5] not in message and " and 2 more." in message


# The ticket's check on a cast fixture -----------------------------------------


def cast_settings(case, **extra) -> None:
    service = case["services"]["workspace_settings"]
    current = service.get(case["session_id"], "tts")
    service.patch(case["session_id"], "tts", current["revision"], {
        "service": "gemini", "model": "gemini-2.5-flash-tts", "voice": "Kore",
        "casting_enabled": True, "performance_enabled": True, **extra,
    })


def live_snapshot(case) -> dict:
    snapshot, _digest = case["services"]["workspace_settings"].resolve(
        case["session_id"], sections=["tts", "audio"]
    )
    return snapshot


def identities(case) -> dict[str, dict]:
    snapshot = live_snapshot(case)
    with case["services"]["database"].session() as session:
        context = AudioIdentityContext(session, snapshot)
        return {
            segment_id: context.for_segment(session.get_one(m.GenerationSegment, segment_id))
            for segment_id in case["segment_ids"]
        }


def complete_every_segment(case) -> None:
    """Give every segment a completed take recorded with today's identity."""
    recorded = identities(case)
    assert all("performance_error" not in value for value in recorded.values()), recorded
    sid = case["session_id"]
    with case["services"]["database"].session() as session:
        ownership = resolve_artifact_ownership(session, session_id=sid)
        for segment_id in case["segment_ids"]:
            artifact = m.Artifact(
                session_id=sid, workspace_id=ownership.workspace_id,
                project_id=ownership.project_id, kind="audio", role="generation_take",
                relative_path=f"test/{segment_id}.wav", content_hash="a" * 64,
                metadata_json={IDENTITY_KEY: recorded[segment_id]},
            )
            session.add(artifact)
            session.flush()
            session.get_one(m.GenerationSegment, segment_id).status = "completed"
            session.add(m.AudioTake(
                generation_segment_id=segment_id, artifact_id=artifact.id,
                status="completed", is_active=True, duration_ms=100,
            ))


@contextmanager
def counted_freezes():
    calls = {"freeze": 0, "validity": 0}
    real_freeze = plans.freeze_performance_snapshot
    real_validity = validity.plan_validity

    def freeze(*args, **kwargs):
        calls["freeze"] += 1
        return real_freeze(*args, **kwargs)

    def judge(*args, **kwargs):
        calls["validity"] += 1
        return real_validity(*args, **kwargs)

    with pytest.MonkeyPatch.context() as patcher:
        patcher.setattr(plans, "freeze_performance_snapshot", freeze)
        patcher.setattr(plans, "plan_validity", judge)
        yield calls


def reasons(case) -> tuple[dict[str, str], dict[str, int]]:
    with counted_freezes() as calls:
        listing = case["services"]["generation"].list_segments(
            case["session_id"], fields=["id", "ordinal", "audio_reuse_reason"]
        )
    return {item["id"]: item["audio_reuse_reason"] for item in listing["items"]}, dict(calls)


def edit_segment(case, index: int, text: str) -> None:
    segment_id = case["segment_ids"][index]
    with case["services"]["database"].session() as session:
        revision = session.get_one(m.GenerationSegment, segment_id).revision
    result = case["services"]["generation"].update_segment(segment_id, revision, {"text": text})
    assert result["id"] == segment_id  # edited in place, not through an edit copy


def start_run(case, segment_ids=None):
    body = {"speech_plan_revision_id": case["revision_id"]}
    if segment_ids is not None:
        body["segment_ids"] = segment_ids
    return case["client"].post(
        f"/api/v1/sessions/{case['session_id']}/generation-runs", json=body,
        headers={**case["headers"], "Idempotency-Key": str(uuid.uuid4())},
    )


# A listing pass freezes the adopted plan a fixed number of times, whatever
# the segment count: a per-segment judgement would multiply it.
LISTING_FREEZES = 1
# The edit resets the flagged segment itself, so its own take is not reused.
FLAGGED_REASON = "not_completed"


@pytest.fixture
def cast_case(case):
    plan, _xml = seed_cast(case)
    cast_settings(case)
    complete_every_segment(case)
    before, work = reasons(case)
    assert set(before.values()) == {"reusable"}, before
    return case, plan, before, work


def test_fitting_edit_keeps_every_other_reuse_reason(cast_case):
    case, plan, before, work = cast_case
    edit_segment(case, 1, "Yet this defeat would be brief.")
    after, work_after = reasons(case)
    others = [sid for sid in case["segment_ids"] if sid != case["segment_ids"][1]]
    assert {sid: after[sid] for sid in others} == {sid: before[sid] for sid in others}
    assert after[case["segment_ids"][1]] == "not_completed"
    # The judgement is made once per listing pass, never once per segment.
    assert work["freeze"] == LISTING_FREEZES
    assert work_after["freeze"] == work["freeze"]
    assert work_after["validity"] == work_after["freeze"]
    read = get(case, plan)
    assert read["stale"] is True
    assert (read["changed_segment_count"], read["review_segment_count"]) == (1, 0)
    current = identities(case)
    assert all("performance_error" not in value for value in current.values())


def test_editing_the_dialogue_segment_inside_its_quote_keeps_its_markup(cast_case):
    case, plan, before, _work = cast_case
    edit_segment(case, 0, "He said, “Pah!” Then left.")
    after, _ = reasons(case)
    assert {sid: after[sid] for sid in case["segment_ids"][1:]} == {
        sid: before[sid] for sid in case["segment_ids"][1:]
    }
    snapshot = live_snapshot(case)
    with case["services"]["database"].session() as session:
        freeze_speech_snapshot(session, case["revision_id"], snapshot)
    record = snapshot["performance_snapshot"]["annotations"][case["segment_ids"][0]]["annotation"]
    assert "“Pah!”</speaker></dialogue>" in record_markup(record)
    cast = snapshot["generation_control_snapshot"]["segments"][case["segment_ids"][0]]
    assert "“Pah!”</speaker></dialogue>" in cast["speech_xml"]
    assert get(case, plan)["review_segment_count"] == 0


def test_edit_that_no_longer_fits_flags_only_that_segment(cast_case):
    case, plan, before, work = cast_case
    flagged = case["segment_ids"][0]
    edit_segment(case, 0, "He said, “Bah,” then left.")
    after, work_after = reasons(case)
    others = case["segment_ids"][1:]
    assert {sid: after[sid] for sid in others} == {sid: before[sid] for sid in others}
    assert work_after["freeze"] == work["freeze"] == LISTING_FREEZES
    assert after[flagged] == FLAGGED_REASON
    current = identities(case)
    assert current[flagged]["performance_error"] == REVIEW_MESSAGE
    assert all("performance_error" not in current[sid] for sid in others)
    read = get(case, plan)
    assert read["stale"] is True
    assert (read["changed_segment_count"], read["review_segment_count"]) == (1, 1)
    assert read["review_segments"] == [{"ordinal": 0, "id": flagged}]

    refused = start_run(case)
    assert refused.status_code == 409, refused.get_json()
    error = refused.get_json()["error"]
    assert error["code"] == "generation_unavailable"
    assert f"block 1 ({flagged})" in error["message"]
    refused = start_run(case, [flagged, others[0]])
    assert refused.status_code == 409, refused.get_json()
    started = start_run(case, [others[0]])
    assert started.status_code == 202, started.get_json()


def test_voice_change_pauses_do_not_refuse_a_run_without_the_flagged_segment(cast_case):
    # With a voice-change pause the boundary freeze resolves every segment's
    # voices, including the flagged one outside the run.
    case, _plan, _before, _work = cast_case
    service = case["services"]["workspace_settings"]
    current = service.get(case["session_id"], "audio")
    service.patch(case["session_id"], "audio", current["revision"], {"voice_change_silence_ms": 300})
    flagged = case["segment_ids"][0]
    edit_segment(case, 0, "He said, “Bah,” then left.")
    refused = start_run(case, [flagged])
    assert refused.status_code == 409, refused.get_json()
    assert f"block 1 ({flagged})" in refused.get_json()["error"]["message"]
    started = start_run(case, [case["segment_ids"][2]])
    assert started.status_code == 202, started.get_json()


def test_a_review_block_naming_a_new_character_does_not_stop_a_run_without_it(cast_case):
    # The review block has no frozen entry, so the boundary freeze reads its
    # own markup, which names a character added after adoption.
    from pandrator.web.generation_controls import save_generation_controls

    case, _plan, _before, _work = cast_case
    characters = [
        *CHARACTERS,
        {"id": "c-marley", "display_name": "Marley", "voice_category": "male"},
    ]
    with case["services"]["database"].session() as session:
        save_generation_controls(
            session, case["session_id"], expected_revision=1, characters=characters,
            cast={
                "narrator": {"voice": "Kore"},
                "characters": {"c-scrooge": {"voice": "Puck"}, "c-marley": {"voice": "Charon"}},
            },
        )
    flagged = case["segment_ids"][0]
    edit_segment(case, 0, "He said, “Bah,” then left.")
    with case["services"]["database"].session() as session:
        segment = session.get_one(m.GenerationSegment, flagged)
        segment.speech_plan_json = {
            **(segment.speech_plan_json or {}),
            "speech_xml": (
                f'<segment id="{flagged}">He said, <dialogue><speaker ref="c-marley">'
                "“Bah,”</speaker></dialogue> then left.</segment>"
            ),
        }
    refused = start_run(case, [flagged])
    assert refused.status_code == 409, refused.get_json()
    assert f"block 1 ({flagged})" in refused.get_json()["error"]["message"]
    started = start_run(case, [case["segment_ids"][2]])
    assert started.status_code == 202, started.get_json()


def test_a_neighbour_whose_request_carries_the_edited_text_really_changes(case):
    # With a TTS context mode that sends neighbour text, the neighbour's
    # request includes the edit, so it loses reuse (the epic's assumption).
    seed_cast(case)
    cast_settings(case, tts_context_mode="both")
    complete_every_segment(case)
    before, _ = reasons(case)
    assert set(before.values()) == {"reusable"}
    edit_segment(case, 1, "Yet this defeat would be brief.")
    after, _ = reasons(case)
    assert after[case["segment_ids"][0]] != "reusable"
    assert after[case["segment_ids"][2]] != "reusable"


# Structure, edit copies, restores and previews ---------------------------------


def active_ids(case) -> tuple[str, list[str]]:
    """The selected revision and its block ids, in order."""
    with case["services"]["database"].session() as session:
        selected = session.scalar(
            select(m.GenerationPlan).where(m.GenerationPlan.session_id == case["session_id"])
        )
        assert selected is not None and selected.active_revision_id
        revision_id = selected.active_revision_id
        return revision_id, list(session.scalars(
            select(m.GenerationSegment.id)
            .where(m.GenerationSegment.plan_revision_id == revision_id)
            .order_by(m.GenerationSegment.ordinal)
        ))


def identities_of(case, segment_ids) -> dict[str, dict]:
    snapshot = live_snapshot(case)
    with case["services"]["database"].session() as session:
        context = AudioIdentityContext(session, snapshot)
        return {
            segment_id: context.for_segment(session.get_one(m.GenerationSegment, segment_id))
            for segment_id in segment_ids
        }


def edit_copy(case, index: int, text: str) -> tuple[str, list[str]]:
    """Edit one block of the selected revision through an edit copy.

    Approving the revision first makes the edit copy it, as a reviewed or used
    revision is in the app. Returns the copy and its block ids.
    """
    revision_id, ids = active_ids(case)
    with case["services"]["database"].session() as session:
        if session.get(m.SpeechPlanReview, revision_id) is None:
            session.add(m.SpeechPlanReview(
                revision_id=revision_id, content_hash=plans.plan_signature(session, revision_id),
            ))
        revision = session.get_one(m.GenerationSegment, ids[index]).revision
    result = case["services"]["generation"].update_segment(ids[index], revision, {"text": text})
    copy_id, copy_ids = active_ids(case)
    assert copy_id != revision_id and result["id"] == copy_ids[index]
    return copy_id, copy_ids


def adopted_read(case, revision_id: str) -> dict:
    with case["services"]["database"].session() as session:
        plan = session.scalar(select(m.PerformancePlan).where(
            m.PerformancePlan.plan_revision_id == revision_id,
            m.PerformancePlan.status == "adopted",
        ))
        assert plan is not None
        plan_id = plan.id
    return get(case, {"id": plan_id})


def change_block(case, index: int, **values) -> None:
    with case["services"]["database"].session() as session:
        segment = session.get_one(m.GenerationSegment, case["segment_ids"][index])
        for key, value in values.items():
            setattr(segment, key, value)


def test_a_structure_change_needs_review_whatever_the_text(cast_case):
    case, plan, before, _work = cast_case
    changed = case["segment_ids"][2]
    change_block(case, 2, speaker="B")
    read = get(case, plan)
    assert (read["changed_segment_count"], read["review_segment_count"]) == (1, 1)
    assert read["review_segments"] == [{"ordinal": 2, "id": changed}]
    current = identities(case)
    assert current[changed]["performance_error"] == REVIEW_MESSAGE
    assert all("performance_error" not in current[sid] for sid in case["segment_ids"][:2])
    after, _ = reasons(case)
    assert {sid: after[sid] for sid in case["segment_ids"][:2]} == {
        sid: before[sid] for sid in case["segment_ids"][:2]
    }
    # Voice, pauses and references are read live by identity, not judged here.
    change_block(case, 2, speaker="A")
    change_block(case, 1, silence_after_ms=900)
    assert get(case, plan)["review_segment_count"] == 0


def test_an_edit_copy_keeps_every_other_identity_and_reason(cast_case):
    case, _plan, before, work = cast_case
    old_ids = case["segment_ids"]
    recorded = identities(case)
    copy_id, new_ids = edit_copy(case, 1, "Yet this defeat would be brief.")
    after, work_after = reasons(case)
    current = identities_of(case, new_ids)
    for index, (old, new) in enumerate(zip(old_ids, new_ids, strict=True)):
        if index == 1:
            continue
        assert after[new] == before[old], index
        assert current[new] == recorded[old], index
    assert "performance_error" not in current[new_ids[1]]
    # The copy's carried plan is judged once per listing pass, as in place.
    assert work_after["freeze"] == work["freeze"] == LISTING_FREEZES
    assert work_after["validity"] == work_after["freeze"]
    read = adopted_read(case, copy_id)
    assert read["stale"] is True
    assert (read["changed_segment_count"], read["review_segment_count"]) == (1, 0)


def test_a_second_edit_copy_after_a_run_judges_both_edited_blocks(cast_case):
    case, _plan, _before, _work = cast_case
    copy_id, copy_ids = edit_copy(case, 1, "Yet this defeat would be brief.")
    run = start_run({**case, "revision_id": copy_id}, [copy_ids[2]])
    assert run.status_code == 202, run.get_json()
    # The second edit lands on a revision whose carried plan is already stale.
    second_id, second_ids = edit_copy(case, 0, "He said, “Bah,” then left.")
    assert second_id != copy_id
    read = adopted_read(case, second_id)
    assert (read["changed_segment_count"], read["review_segment_count"]) == (2, 1)
    assert read["review_segments"] == [{"ordinal": 0, "id": second_ids[0]}]
    after, work = reasons(case)
    assert after[second_ids[2]] == "reusable"
    assert after[second_ids[0]] == FLAGGED_REASON
    assert work["freeze"] == LISTING_FREEZES
    current = identities_of(case, second_ids)
    assert current[second_ids[0]]["performance_error"] == REVIEW_MESSAGE
    assert "performance_error" not in current[second_ids[1]]
    assert "performance_error" not in current[second_ids[2]]


def test_restore_of_a_stale_plan_reports_the_sources_blocks(cast_case):
    case, plan, before, _work = cast_case
    recorded = identities(case)
    edit_segment(case, 0, "He said, “Bah,” then left.")
    edit_segment(case, 1, "Yet this defeat would be brief.")
    source = get(case, plan)
    assert (source["changed_segment_count"], source["review_segment_count"]) == (2, 1)
    result = case["services"]["generation"].revise_topology(
        case["session_id"], case["revision_id"],
        {"action": "restore", "target_revision_id": case["revision_id"]},
    )
    assert result["review_required"]["performance"] is True
    lineage = {key: children[0] for key, children in result["lineage"].items()}
    carried = get(case, {"id": result["carried_performance_plan_id"]})
    assert (carried["changed_segment_count"], carried["review_segment_count"]) == (2, 1)
    assert carried["review_segments"] == [
        {"ordinal": 0, "id": lineage[case["segment_ids"][0]]}
    ]
    last = case["segment_ids"][2]
    current = identities_of(case, [lineage[sid] for sid in case["segment_ids"]])
    assert current[lineage[case["segment_ids"][0]]]["performance_error"] == REVIEW_MESSAGE
    assert current[lineage[last]] == recorded[last]
    after, _ = reasons(case)
    assert after[lineage[last]] == before[last]


def preview(case, segment_id: str) -> dict:
    from pandrator.web.speech_plan_preview import preview_speech_segment

    return preview_speech_segment(
        case["services"], case["session_id"],
        revision_id=case["revision_id"], segment_id=segment_id,
    )


def test_block_preview_judges_the_adopted_plan_per_block(cast_case):
    case, _plan, _before, _work = cast_case
    first, middle, last = case["segment_ids"]
    edit_segment(case, 0, "He said, “Pah!” Then left.")
    fitted = preview(case, first)
    assert fitted["text"] == "He said, “Pah!” Then left."
    voiced = [span for span in fitted["spans"] if span["speaker_id"]]
    assert [(span["start"], span["end"], span["speaker_id"]) for span in voiced] == [
        (9, 15, "c-scrooge")
    ]
    assert preview(case, last)["text"] == "Membership doubled soon afterwards."
    edit_segment(case, 1, "Yet this defeat would be brief.")
    assert preview(case, middle)["text"] == "Yet this defeat would be brief."
    edit_segment(case, 0, "He said, “Bah,” then left.")
    with pytest.raises(ValueError, match="no longer fits"):
        preview(case, first)
    assert preview(case, last)["text"] == "Membership doubled soon afterwards."


def test_units_made_before_structure_fields_are_judged_by_their_text(cast_case):
    case, plan, _before, _work = cast_case
    with case["services"]["database"].session() as session:
        stored = session.get_one(m.PerformancePlan, plan["id"])
        stored.units_json = [
            {key: value for key, value in unit.items() if key not in validity.STRUCTURE_FIELDS}
            for unit in stored.units_json
        ]
    edit_segment(case, 1, "Yet this defeat would be brief.")
    # A field the unit never recorded is not judged.
    change_block(case, 2, speaker="B")
    read = get(case, plan)
    assert (read["changed_segment_count"], read["review_segment_count"]) == (1, 0)
    frozen = freeze(case)
    assert set(frozen["annotations"]) == set(case["segment_ids"])
    assert frozen["review_segment_ids"] == []


@pytest.mark.parametrize("mode", ["manual", "passive"])
@pytest.mark.parametrize("edited", [False, True])
def test_a_new_draft_falls_back_to_block_markup_when_characters_changed(cast_case, edited, mode):
    from pandrator.web.generation_controls import save_generation_controls

    case, plan, _before, _work = cast_case
    first, middle, _last = case["segment_ids"]
    # Locked, the dialogue entry would be seeded by reanalysis if it still held.
    with case["services"]["database"].session() as session:
        stored = session.get_one(m.PerformancePlan, plan["id"])
        manual = deepcopy(stored.manual_annotations_json or {})
        manual[first] = {**manual[first], "locked": True}
        stored.manual_annotations_json = manual
    if edited:
        # The plan is stale: its entries are judged per block first.
        edit_segment(case, 1, "Yet this defeat would be brief.")
    with case["services"]["database"].session() as session:
        save_generation_controls(
            session, case["session_id"], expected_revision=1, characters=[], cast={},
        )
    draft = create(case, annotation_format="xml", mode=mode)
    items = {item["id"]: item for item in draft["items"]}
    # The adopted dialogue names a character that no longer exists, so the
    # block starts from its own markup and the entry is not seeded.
    assert "<dialogue>" not in items[first]["speech_xml"]
    assert items[first]["annotation"] is None
    if edited:
        assert items[middle]["speech_xml"].endswith("would be brief.</segment>")
