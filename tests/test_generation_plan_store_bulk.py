"""The plan store builds its segment rows first and inserts them in one statement.

It used to add and flush each segment, then rewrite its speech plan, inside the
caller's write transaction: 5,174 flushes and a 1.9 s writer hold for a
5,167-segment book. The old store is kept below as the reference; the tests
compare the stored rows with it and count statements and markup parses, never
time.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from contextlib import contextmanager, nullcontext
from typing import Any
from unittest.mock import patch

import pytest
from sqlalchemy import event, func, select

from pandrator.logic import speech_markup
from pandrator.web import models as m
from pandrator.web.credentials import redact_inline_secrets
from pandrator.web.generation_controls import save_generation_controls
from pandrator.web.generation_plan_store import (
    GenerationPlanStoreContext,
    store_generation_plan,
)
from tests.test_performance_plans import case as case  # noqa: F401


def reference_store(
    context: GenerationPlanStoreContext, session_id: str, records: list[dict[str, Any]], *,
    settings: dict[str, Any], db_session,
) -> tuple[str, list[str]]:
    """The store before the bulk insert: one add, flush and rewrite per segment."""
    from pandrator.logic.speech_markup import parse_speech_markup
    from pandrator.web.generation_cast_runtime import remap_markup
    from pandrator.web.generation_controls import get_generation_controls
    from pandrator.web.speech_boundaries import effective_speech_boundary, record_pause_metadata

    clean = [item for item in records if str(item.get("text") or item.get("original_sentence") or "").strip()]
    digest = hashlib.sha256(json.dumps({
        "records": clean, "settings": context._generation_segmentation_settings(settings),
        "source_revision_id": None, "source_artifact_id": None,
    }, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")).hexdigest()
    with nullcontext(db_session) as session:
        plan = session.scalar(select(m.GenerationPlan).where(m.GenerationPlan.session_id == session_id))
        if plan is None:
            plan = m.GenerationPlan(session_id=session_id)
            session.add(plan)
            session.flush()
        maximum = session.scalar(select(func.max(m.GenerationPlanRevision.revision_number)).where(
            m.GenerationPlanRevision.plan_id == plan.id)) or 0
        revision = m.GenerationPlanRevision(
            plan_id=plan.id, parent_revision_id=plan.active_revision_id, source_revision_id=None,
            revision_number=int(maximum) + 1,
            settings_json=redact_inline_secrets(context._secret_free_tts_settings(settings)),
            content_hash=digest,
        )
        session.add(revision)
        session.flush()
        segment_ids = []
        characters = get_generation_controls(session, session_id)["characters"]
        for ordinal, record in enumerate(clean):
            record = dict(record)
            source_markup = record.get("speech_xml") or (record.get("speech_plan") or {}).get("speech_xml")
            if source_markup:
                spoken = str(record.get("tts_optimized_sentence") or record.get("text")
                             or record.get("original_sentence") or "").strip()
                normalized = remap_markup(source_markup, str(ordinal + 1), spoken, characters)
                structure = parse_speech_markup(normalized, expected_segment_id=str(ordinal + 1),
                                                expected_text=spoken, characters=characters)
                boundary = effective_speech_boundary(structure)
                if boundary is not None:
                    record["speech_boundary_after"] = boundary
                if record.get("speech_boundary_after") in {"continuation", "dialogue_turn"}:
                    record["paragraph_break_after"] = False
                    record["paragraph"] = False
            is_subtitle = context._is_subtitle_generation_record(record)
            segment = m.GenerationSegment(
                plan_revision_id=revision.id, ordinal=ordinal,
                source_segment_ids_json=list(record.get("source_segment_ids") or record.get("subtitles") or []),
                speech_block_provenance_json=dict(record.get("speech_block_provenance") or record.get("provenance") or {}),
                alignment_group=str(record.get("alignment_group") or "").strip() or None,
                node_kind=str(record.get("node_kind") or (
                    "subtitle_cue" if is_subtitle
                    else "chapter_marker" if str(record.get("chapter") or "").lower() == "yes"
                    else "paragraph")),
                paragraph_break_after=False if is_subtitle else bool(record.get(
                    "paragraph_break_after", str(record.get("paragraph") or "").lower() == "yes")),
                speaker=str(record.get("speaker") or "").strip() or None,
                text=str(record.get("text") or record.get("original_sentence") or "").strip(),
                optimized_text=(str(record.get("tts_optimized_sentence") or "").strip() or None),
                speech_plan_json=dict(record.get("speech_plan") or {}),
                optimization_status=("optimized" if str(record.get("tts_optimized_sentence") or "").strip()
                                     else "not_requested"),
                optimization_source_hash=(
                    context._optimization_text_hash(str(record.get("text") or record.get("original_sentence") or "").strip())
                    if str(record.get("tts_optimized_sentence") or "").strip() else None),
                optimization_model=(str((record.get("speech_plan") or {}).get("model") or "").strip() or None),
                voice_id=record.get("voice_id"),
                voice=str(record.get("voice") or "").strip() or None,
                language=context._usable_language(record.get("language")) or None,
                silence_after_ms=context._default_silence_after_ms(record, settings, is_subtitle=is_subtitle),
                marked=bool(record.get("marked", False)),
            )
            session.add(segment)
            session.flush()
            segment.speech_plan_json = {**(segment.speech_plan_json or {}),
                                        **record_pause_metadata(record, is_subtitle=is_subtitle)}
            if source_markup:
                segment.speech_plan_json = {**(segment.speech_plan_json or {}), "speech_xml": remap_markup(
                    source_markup, segment.id, segment.optimized_text or segment.text, characters)}
            segment_ids.append(segment.id)
        plan.active_revision_id = revision.id
        return revision.id, segment_ids


def _records() -> list[dict[str, Any]]:
    def xml(inner: str, **attributes: str) -> str:
        extra = "".join(f' {key}="{value}"' for key, value in attributes.items())
        return f'<segment id="1"{extra}>{inner}</segment>'

    return [
        {"original_sentence": "“Hello.”", "paragraph": "yes",
         "speech_xml": xml("<dialogue>“Hello.”</dialogue>", boundary_after="dialogue_turn")},
        {"text": "He said, “Bah!” Then left.", "speaker": "Narrator",
         "speech_xml": xml('He said, <dialogue><speaker ref="fred">“Bah!”</speaker></dialogue> Then left.')},
        {"text": "Plain paragraph.", "paragraph": "yes", "language": "de", "marked": True},
        {"text": "   "},
        {"text": "Chapter One", "chapter": "yes", "voice": " narrator-two "},
        {"text": "Spoken differently.", "tts_optimized_sentence": "Spoken, differently.",
         "speech_plan": {"model": "planner", "speech_xml": xml("<em>calm</em>Spoken, differently.")}},
        {"text": "Grouped.", "source_segment_ids": [3, 4], "alignment_group": " g1 ",
         "provenance": {"source": "test"}},
    ]


def _session_with_fred(case) -> str:  # noqa: F811
    services = case["services"]
    created = case["client"].post(
        "/api/v1/sessions", json={"name": f"Bulk store {uuid.uuid4().hex[:8]}", "workflow_kind": "audiobook"},
        headers=case["headers"],
    )
    assert created.status_code == 201, created.get_json()
    session_id = created.get_json()["id"]
    with services["database"].session() as session:
        save_generation_controls(
            session, session_id, expected_revision=0,
            characters=[{"id": "fred", "display_name": "Fred", "voice_category": "male"}],
        )
    return session_id


def _rows(case, revision_id: str) -> list[dict[str, Any]]:  # noqa: F811
    ignored = {"id", "plan_revision_id", "created_at", "updated_at"}
    with case["services"]["database"].session() as session:
        segments = list(session.scalars(
            select(m.GenerationSegment)
            .where(m.GenerationSegment.plan_revision_id == revision_id)
            .order_by(m.GenerationSegment.ordinal)
        ))
        rows = []
        for segment in segments:
            row = {
                column.key: getattr(segment, column.key)
                for column in m.GenerationSegment.__table__.columns
                if column.key not in ignored
            }
            plan = dict(row["speech_plan_json"])
            if "speech_xml" in plan:
                assert f'id="{segment.id}"' in plan["speech_xml"]
                plan["speech_xml"] = re.sub(r'id="[^"]+"', 'id="SEGMENT"', plan["speech_xml"], count=1)
            row["speech_plan_json"] = plan
            rows.append(row)
        return rows


@contextmanager
def _statements(case):  # noqa: F811
    recorded: list[str] = []

    def record(_connection, _cursor, statement, _parameters, _context, _many):
        recorded.append(statement)

    engine = case["services"]["database"].engine
    event.listen(engine, "before_cursor_execute", record)
    try:
        yield recorded
    finally:
        event.remove(engine, "before_cursor_execute", record)


def _writes(statements: list[str], verb: str) -> int:
    return sum(1 for text in statements if text.startswith(f"{verb} generation_segments")
               or text.startswith(f"{verb} INTO generation_segments"))


SETTINGS = {"paragraph_silence_ms": 700, "sentence_silence_ms": 120}


def test_bulk_store_gives_the_reference_rows_with_one_insert(case):  # noqa: F811
    handlers = case["services"]["workflow_handlers"]
    context = handlers._generation_plan_store_context()
    database = case["services"]["database"]
    reference_session, session_id = _session_with_fred(case), _session_with_fred(case)
    with database.immediate_session() as unit:
        reference_id, _ = reference_store(context, reference_session, _records(), settings=SETTINGS, db_session=unit)
    with _statements(case) as statements, database.immediate_session() as unit:
        revision_id, ids = store_generation_plan(context, session_id, _records(), settings=SETTINGS, db_session=unit)
    assert len(ids) == 6
    assert _rows(case, revision_id) == _rows(case, reference_id)
    # One INSERT for every segment and no per-segment UPDATE of the speech plan.
    assert (_writes(statements, "INSERT"), _writes(statements, "UPDATE")) == (1, 0)


def test_prepared_rows_are_stored_without_parsing_markup_in_the_writer(case):  # noqa: F811
    handlers = case["services"]["workflow_handlers"]
    context = handlers._generation_plan_store_context()
    database = case["services"]["database"]
    session_id = _session_with_fred(case)
    records = _records()
    prepared = handlers._prepare_generation_plan_rows(session_id, records, settings=SETTINGS)
    with patch.object(speech_markup, "parse_speech_markup", wraps=speech_markup.parse_speech_markup) as parse, \
            database.immediate_session() as unit:
        revision_id, ids = store_generation_plan(
            context, session_id, records, settings=SETTINGS, db_session=unit, prepared_rows=prepared,
        )
    parse.assert_not_called()
    assert ids == [row["id"] for row in prepared.rows]
    reference_session = _session_with_fred(case)
    with database.immediate_session() as unit:
        reference_id, _ = reference_store(context, reference_session, _records(), settings=SETTINGS, db_session=unit)
    assert _rows(case, revision_id) == _rows(case, reference_id)


def test_prepared_rows_are_rebuilt_when_the_characters_changed(case):  # noqa: F811
    handlers = case["services"]["workflow_handlers"]
    context = handlers._generation_plan_store_context()
    database = case["services"]["database"]
    session_id = _session_with_fred(case)
    records = _records()
    prepared = handlers._prepare_generation_plan_rows(session_id, records, settings=SETTINGS)
    with database.session() as session:
        save_generation_controls(
            session, session_id, expected_revision=1,
            characters=[{"id": "fred", "display_name": "Frederick", "voice_category": "male"}],
        )
    with patch.object(speech_markup, "parse_speech_markup", wraps=speech_markup.parse_speech_markup) as parse, \
            database.immediate_session() as unit:
        _revision_id, ids = store_generation_plan(
            context, session_id, records, settings=SETTINGS, db_session=unit, prepared_rows=prepared,
        )
    assert parse.call_count == 3  # once per record with markup, rebuilt under the writer
    assert ids != [row["id"] for row in prepared.rows]


def _store(case, session_id, records, prepared, *, settings=SETTINGS, force_new=False):  # noqa: F811
    context = case["services"]["workflow_handlers"]._generation_plan_store_context()
    with patch.object(
        speech_markup, "parse_speech_markup", wraps=speech_markup.parse_speech_markup,
    ) as parse, case["services"]["database"].immediate_session() as unit:
        revision_id, ids = store_generation_plan(
            context, session_id, records, settings=settings, db_session=unit,
            prepared_rows=prepared, force_new=force_new,
        )
    return revision_id, ids, parse.call_count


def test_prepared_rows_for_equal_records_in_another_list_are_used(case):  # noqa: F811
    handlers = case["services"]["workflow_handlers"]
    session_id = _session_with_fred(case)
    prepared = handlers._prepare_generation_plan_rows(session_id, _records(), settings=SETTINGS)
    _revision_id, ids, parses = _store(case, session_id, _records(), prepared)
    assert parses == 0
    assert ids == [row["id"] for row in prepared.rows]


def test_prepared_rows_are_rebuilt_when_their_records_changed_since(case):  # noqa: F811
    handlers = case["services"]["workflow_handlers"]
    session_id = _session_with_fred(case)
    records = _records()
    prepared = handlers._prepare_generation_plan_rows(session_id, records, settings=SETTINGS)
    # The same list, changed in place after the rows were built.
    records[0] = {**records[0], "voice": "changed-voice"}
    revision_id, ids, parses = _store(case, session_id, records, prepared)
    assert parses == 3  # rebuilt under the writer
    assert ids != [row["id"] for row in prepared.rows]
    assert _rows(case, revision_id)[0]["voice"] == "changed-voice"
    reference_session = _session_with_fred(case)
    context = handlers._generation_plan_store_context()
    with case["services"]["database"].immediate_session() as unit:
        reference_id, _ = reference_store(
            context, reference_session, records, settings=SETTINGS, db_session=unit,
        )
    assert _rows(case, revision_id) == _rows(case, reference_id)


def test_prepared_rows_are_rebuilt_for_other_settings(case):  # noqa: F811
    handlers = case["services"]["workflow_handlers"]
    session_id = _session_with_fred(case)
    records = _records()
    prepared = handlers._prepare_generation_plan_rows(session_id, records, settings=SETTINGS)
    other = {**SETTINGS, "paragraph_silence_ms": 1500}
    _revision_id, ids, parses = _store(case, session_id, records, prepared, settings=other)
    assert parses == 3
    assert ids != [row["id"] for row in prepared.rows]


def test_committed_prepared_rows_are_not_inserted_again(case):  # noqa: F811
    handlers = case["services"]["workflow_handlers"]
    session_id = _session_with_fred(case)
    records = _records()
    prepared = handlers._prepare_generation_plan_rows(session_id, records, settings=SETTINGS)
    first, first_ids, first_parses = _store(case, session_id, records, prepared)
    assert (first_parses, first_ids) == (0, [row["id"] for row in prepared.rows])
    assert prepared.consumed
    # A second, forced store would reuse segment ids that now exist.
    second, second_ids, second_parses = _store(case, session_id, records, prepared, force_new=True)
    assert second != first
    assert second_parses == 3
    assert not set(second_ids) & set(first_ids)
    assert _rows(case, second) == _rows(case, first)


def test_prepared_rows_stay_usable_after_a_rolled_back_store(case):  # noqa: F811
    handlers = case["services"]["workflow_handlers"]
    context = handlers._generation_plan_store_context()
    session_id = _session_with_fred(case)
    records = _records()
    prepared = handlers._prepare_generation_plan_rows(session_id, records, settings=SETTINGS)
    with pytest.raises(RuntimeError), case["services"]["database"].immediate_session() as unit:
        store_generation_plan(
            context, session_id, records, settings=SETTINGS, db_session=unit, prepared_rows=prepared,
        )
        raise RuntimeError("a later step of the same transaction fails")
    assert not prepared.consumed
    _revision_id, ids, parses = _store(case, session_id, records, prepared)
    assert parses == 0
    assert ids == [row["id"] for row in prepared.rows]
    assert prepared.consumed


def test_identical_content_keeps_the_active_revision(case):  # noqa: F811
    handlers = case["services"]["workflow_handlers"]
    session_id = _session_with_fred(case)
    first, first_ids = handlers._store_generation_plan(session_id, _records(), settings=SETTINGS)
    records = _records()
    prepared = handlers._prepare_generation_plan_rows(session_id, records, settings=SETTINGS)
    second, second_ids = handlers._store_generation_plan(
        session_id, records, settings=SETTINGS, prepared_rows=prepared,
    )
    assert (second, second_ids) == (first, first_ids)
