"""Cross-stage passage identity and real speech timing, without model or TTS calls."""

from copy import deepcopy
from threading import Event
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from sqlalchemy import select

from pandrator.logic.dubbing.subtitle_projection import project_subtitle_display
from pandrator.web.logical_passages import (
    attach_passages,
    map_output_passages,
    materialize_speech_source,
    passage_srt,
    source_passages,
    stored_passages,
)
from pandrator.web.models import (
    Artifact,
    AudioTake,
    DispatchRun,
    Document,
    DocumentRevision,
    GenerationRun,
    Segment,
    TimedWord,
)
from pandrator.web.voiceover_repair import _load_groups
from tests import test_dubbing_llm_translation as translation_tests
from tests import test_web_dispatch as dispatch_tests


@pytest.fixture
def app_case():
    case = dispatch_tests.DispatchWebTests(methodName="runTest")
    case.setUp()
    try:
        yield case
    finally:
        case.tearDown()


def register_rows(
    case, session_id, rows, *, role="transcription", parent=None, name=None
):
    record = case.extension["sessions"].get(session_id)
    path = (
        case.extension["paths"].sessions / record.storage_key / (name or f"{role}.srt")
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(passage_srt(rows), encoding="utf-8")
    artifact = case.extension["artifacts"].register(
        path,
        kind="srt",
        role=role,
        session_id=session_id,
        parent_ids=[parent.id] if parent else [],
    )
    case.extension["workflow_handlers"]._store_srt_document(
        session_id,
        artifact,
        role,
        language="de" if role == "translation" else "en",
        parent_artifact=parent,
        speaker_overrides={
            index + 1: row.get("speaker", "") for index, row in enumerate(rows)
        },
    )
    return case.extension["artifacts"].resolve(artifact.id)


def source(case):
    record = case.extension["sessions"].create(
        "Logical passages",
        workflow_kind="voiceover",
        source_language="en",
        target_language="de",
    )
    rows = [
        {
            "id": "a",
            "start_ms": 0,
            "end_ms": 6000,
            "text": "One meaningful clause,",
            "speaker": "SPEAKER_00",
        },
        {
            "id": "b",
            "start_ms": 6500,
            "end_ms": 13680,
            "text": "followed by another clause.",
            "speaker": "SPEAKER_00",
        },
        {
            "id": "c",
            "start_ms": 14000,
            "end_ms": 19000,
            "text": "A separate final thought.",
            "speaker": "SPEAKER_00",
        },
    ]
    artifact, path = register_rows(case, record.id, rows)
    return record.id, artifact, path


def submit(case, claim, result):
    response = case.client.post(
        f"/api/v1/dispatch-batches/{claim['batch_id']}/submit",
        json={"lease_token": claim["lease_token"], "result": result},
        headers=case._headers("submit-" + claim["batch_id"]),
    )
    assert response.status_code == 200, response.get_json()
    assert response.get_json()["run_status"] == "completed"
    with case.extension["database"].session() as session:
        run = session.get(DispatchRun, claim["run_id"])
        output_id = run.result_artifact_id
    return case.extension["artifacts"].resolve(output_id)


def test_model_merges_remain_atomic_through_translation_and_speech(app_case):
    case = app_case
    session_id, artifact, _ = source(case)
    run = case._create(session_id, source_artifact_id=artifact.id)
    claim = case._claim(run["id"])
    assert claim["batch"]["id_namespace"] == "logical_passage"
    assert claim["batch"]["cues"][0]["evidence_cue_ids"] == [1]
    corrected, _ = submit(
        case,
        claim,
        {
            "kind": "correction",
            "operations": [
                {
                    "action": "merge",
                    "cue_ids": [1, 2],
                    "texts": ["One meaningful clause, followed by another clause."],
                },
            ],
        },
    )
    passages = stored_passages(corrected)
    assert len(passages) == 2
    assert (passages[0]["start_ms"], passages[0]["end_ms"]) == (0, 13680)
    assert len(passages[0]["source_passage_ids"]) == 2

    translated_run = case._create(
        session_id, kind="translation", source_artifact_id=corrected.id
    )
    translated_claim = case._claim(translated_run["id"], key="claim-translation")
    assert [cue["text"] for cue in translated_claim["batch"]["cues"]] == [
        row["text"] for row in passages
    ]
    translated, path = submit(
        case,
        translated_claim,
        {
            "kind": "translation",
            "translations": [
                {
                    "cue_id": 1,
                    "text": "Ein sinnvoller Teilsatz, gefolgt von einem weiteren Teilsatz.",
                },
                {"cue_id": 2, "text": "Ein eigener abschließender Gedanke."},
            ],
        },
    )
    assert len(stored_passages(translated)) == 2
    records, source_id, _ = case.extension[
        "workflow_handlers"
    ]._subtitle_generation_records(
        translated,
        path,
        {"min_speech_block_chars": 300, "max_speech_block_chars": 500},
        "de",
    )
    cues = [cue for item in records for cue in item["provenance"]["source_cues"]]
    assert [(cue["start_ms"], cue["end_ms"]) for cue in cues] == [
        (0, 13680),
        (14000, 19000),
    ]
    assert all(cue["start_ms"] != 6500 for cue in cues)
    # An oversized merged passage uses natural chunks sharing the original
    # window; no internal estimates or obsolete pre-merge anchors are revived.
    limited, _, _ = case.extension["workflow_handlers"]._subtitle_generation_records(
        translated, path, {"speech_block_max_chars": 40}, "de"
    )
    pieces = [row for row in limited if row["source_segment_ids"] == [1]]
    assert len(pieces) > 1
    assert len({row["alignment_group"] for row in pieces}) == 1
    assert all("shared_passage_timing" in row["provenance"]["risk_flags"] for row in pieces)
    assert all("estimated_internal_timing" not in row["provenance"]["risk_flags"] for row in pieces)
    assert all(
        (cue["start_ms"], cue["end_ms"]) == (0, 13680)
        for row in pieces for cue in row["provenance"]["source_cues"]
    )
    assert all(
        item["provenance"]["source_reference_namespace"] == "logical_passage_ordinal"
        for item in records
    )
    with case.extension["database"].session() as session:
        document = session.get(Document, translated.metadata_json["document_id"])
        assert document.active_revision_id == translated.metadata_json["revision_id"]
        assert source_id != document.active_revision_id
        assert all(
            row.node_kind == "logical_passage"
            for row in session.scalars(
                select(Segment).where(Segment.revision_id == source_id)
            )
        )
    history = case.client.get(f"/api/v1/sessions/{session_id}/documents").get_json()
    assert source_id not in [
        revision["id"] for item in history["items"] for revision in item["revisions"]
    ]


def test_translation_merge_uses_combined_window_without_a_duration_cap(app_case):
    case = app_case
    session_id, artifact, _ = source(case)
    run = case._create(session_id, kind="translation", source_artifact_id=artifact.id)
    claim = case._claim(run["id"])
    translated, _ = submit(
        case,
        claim,
        {
            "kind": "translation",
            "translations": [
                {
                    "cue_ids": [1, 2],
                    "text": "Ein zusammenhängender Gedanke aus beiden Teilsätzen.",
                },
                {"cue_id": 3, "text": "Ein abschließender Gedanke."},
            ],
        },
    )
    rows = stored_passages(translated)
    assert len(rows) == 2
    assert (rows[0]["start_ms"], rows[0]["end_ms"]) == (0, 13680)
    assert len(rows[0]["source_passage_ids"]) == 2


def test_display_width_changes_do_not_change_speech_or_repair_sources(app_case):
    case = app_case
    session_id, original, _ = source(case)
    logical = [
        {
            "id": "p1",
            "start_ms": 0,
            "end_ms": 13680,
            "text": "This is a naturally merged passage with enough wording to wrap into several different subtitle cards when the display width changes.",
            "speaker": "SPEAKER_00",
            "source_passage_ids": ["old-a", "old-b"],
        },
        {
            "id": "p2",
            "start_ms": 14000,
            "end_ms": 19000,
            "text": "This is the final thought.",
            "speaker": "SPEAKER_00",
        },
    ]
    handlers = case.extension["workflow_handlers"]
    plans = []
    display_counts = []
    for width in (20, 90):
        display = project_subtitle_display(
            logical, {"subtitle_max_chars_per_line": width}
        )
        display_counts.append(len(display))
        output, path = register_rows(
            case,
            session_id,
            display,
            role="translation",
            parent=original,
            name=f"width-{width}.srt",
        )
        with case.extension["database"].session() as session:
            attach_passages(session.get(Artifact, output.id), logical, source=original)
        records, source_id, _ = handlers._subtitle_generation_records(
            output, path, {}, "en"
        )
        plans.append(records)
        again = handlers._subtitle_generation_records(output, path, {}, "en")
        assert again[1] == source_id
        revision_id, segment_ids = handlers._store_generation_plan(
            session_id, records, settings={}, source_revision_id=source_id
        )
        with case.extension["database"].session() as session:
            run = GenerationRun(
                session_id=session_id,
                plan_revision_id=revision_id,
                sequence_number=width,
                status="completed",
            )
            session.add(run)
            session.flush()
            run_id = run.id
        for index, segment_id in enumerate(segment_ids):
            audio_path = path.parent / f"take-{width}-{index}.wav"
            audio_path.write_bytes(b"placeholder; duration is not read by _load_groups")
            audio = case.extension["artifacts"].register(
                audio_path, kind="audio", role="tts_segment", session_id=session_id
            )
            with case.extension["database"].session() as session:
                session.add(
                    AudioTake(
                        generation_segment_id=segment_id,
                        generation_run_id=run_id,
                        artifact_id=audio.id,
                        status="completed",
                    )
                )
        groups, takes = _load_groups(handlers, run_id)
        assert len(takes) == len(records)
        assert groups[0].start_ms == 0
        assert groups[-1].end_ms == 19000
    assert display_counts[0] > display_counts[1]
    assert plans[0] == plans[1]


def test_source_words_provide_passages_and_distinct_audio_evidence_ids(app_case):
    case = app_case
    session_id, source_id = case._source(texts=("First sentence. Second sentence.",))
    with case.extension["database"].session() as session:
        artifact = session.get(Artifact, source_id)
        cue = session.scalar(
            select(Segment).where(
                Segment.revision_id == artifact.metadata_json["revision_id"]
            )
        )
        for index, text in enumerate(cue.text.split()):
            session.add(
                TimedWord(
                    revision_id=cue.revision_id,
                    segment_id=cue.id,
                    ordinal=index,
                    text=text,
                    start_ms=index * 250,
                    end_ms=(index + 1) * 250,
                )
            )
    claim = case._claim(case._create(session_id)["id"])
    cues = claim["batch"]["cues"]
    assert [cue["text"] for cue in cues] == ["First sentence.", "Second sentence."]
    assert [cue["cue_id"] for cue in cues] == [1, 2]
    assert [cue["evidence_cue_ids"] for cue in cues] == [[1], [1]]


def test_stale_ledger_is_ignored_after_display_revision_changes(app_case):
    case = app_case
    _, original, _ = source(case)
    database = case.extension["database"]
    with database.session() as session:
        managed = session.get(Artifact, original.id)
        rows = source_passages(session, managed)
        attach_passages(managed, rows, source=managed)
        assert stored_passages(managed) == rows
        managed.metadata_json = {
            **managed.metadata_json,
            "revision_id": "a-different-revision",
        }
        assert stored_passages(managed) is None
        assert materialize_speech_source(session, managed) is None


def test_fork_rebinds_passages_and_materializes_its_own_source(app_case):
    case = app_case
    session_id, original, _ = source(case)
    with case.extension["database"].session() as session:
        rows = source_passages(session, session.get(Artifact, original.id))
    original, _ = register_rows(
        case, session_id, rows, role="correction", parent=original
    )
    database = case.extension["database"]
    with database.immediate_session() as session:
        managed = session.get(Artifact, original.id)
        rows = source_passages(session, managed)
        attach_passages(managed, rows, source=managed)
        _, old_source_id = materialize_speech_source(session, managed)
    response = case.client.post(
        f"/api/v1/sessions/{session_id}/forks",
        json={"checkpoint_artifact_id": original.id},
        headers=case._headers(),
    )
    assert response.status_code == 201, response.get_json()
    fork = response.get_json()
    with database.immediate_session() as session:
        copied = session.get(Artifact, fork["checkpoint_artifact_id"])
        assert stored_passages(copied) == rows
        assert (
            "speech_source_revision_id" not in copied.metadata_json["logical_passages"]
        )
        _, new_source_id = materialize_speech_source(session, copied)
        assert new_source_id != old_source_id
        revision = session.get(DocumentRevision, new_source_id)
        assert session.get(Document, revision.document_id).session_id == fork["id"]


def test_automatic_stage_projection_retains_raw_passages(app_case):
    case = app_case
    _, original, path = source(case)
    handlers = case.extension["workflow_handlers"]
    processing_path, rows, _ = handlers._prepare_passage_input(
        original, path, path.parent
    )
    assert processing_path != path
    output = path.with_name("corrected-model-output.srt")
    result = SimpleNamespace(
        output_path=str(output),
        logical_passages=[
            {
                "text": "A naturally corrected complete thought with all its original meaning.",
                "start_ms": 0,
                "end_ms": 13680,
                "source_cue_ids": [1, 2],
                "speaker": "SPEAKER_00",
            },
            {
                "text": rows[2]["text"],
                "start_ms": 14000,
                "end_ms": 19000,
                "source_cue_ids": [3],
                "speaker": "SPEAKER_00",
            },
        ],
    )
    rendered, _ = handlers._render_passage_output(
        original,
        result,
        rows,
        {"_logical_passage_display": {"subtitle_max_chars_per_line": 20}},
        "en",
    )
    assert rendered == map_output_passages(result.logical_passages, rows)
    assert len(rendered) == 2
    assert output.is_file()
    assert len(output.read_text().split(" --> ")) - 1 > len(rendered)
    assert stored_passages(original) is None
    assert path.read_text() != output.read_text()


def test_legacy_pending_dispatch_keeps_the_original_contract(app_case):
    case = app_case
    session_id, _ = case._source()
    run = case._create(session_id)
    with case.extension["database"].session() as session:
        managed = session.get(DispatchRun, run["id"])
        settings = deepcopy(managed.settings_json)
        settings.pop("_logical_passages_version")
        managed.settings_json = settings
    claim = case._claim(run["id"])
    assert claim["batch"]["id_namespace"] == "source_revision_cue"
    output, _ = submit(case, claim, {"kind": "correction", "operations": []})
    assert stored_passages(output) is None


def test_native_workflow_passes_logical_rows_between_models_and_preserves_review(
    app_case,
):
    case = app_case
    session_id, original, _ = source(case)
    handlers = case.extension["workflow_handlers"]
    with case.extension["database"].session() as session:
        cue = session.scalar(
            select(Segment).where(
                Segment.revision_id == original.metadata_json["revision_id"],
                Segment.ordinal == 1,
            )
        )
        cue.metadata_json = {
            "review_state": "uncertain",
            "review_note": "Check the name.",
            "evidence_ids": ["source-evidence"],
            "uncertain_source_cue_ids": [2],
        }
    defaults = {
        **translation_tests._settings(),
        "correction_model": "anthropic/claude-sonnet-4-6",
        "target_language": "de",
    }
    with patch.object(
        handlers,
        "_with_database_llm_settings",
        side_effect=lambda settings, _stage: {**defaults, **settings},
    ):
        with patch(
            "pandrator.logic.llm_handler.chat_completion_with_metadata",
            return_value='{"operations":[{"action":"merge","cue_ids":[1,2],"texts":["A complete corrected thought combining both clauses."]}]}',
        ):
            correction = handlers.correct(
                {"session_id": session_id, "source_artifact_id": original.id},
                lambda *_args: None,
                Event(),
            )
        corrected, _ = case.extension["artifacts"].resolve(correction["artifact_id"])
        rows = stored_passages(corrected)
        assert len(rows) == 2
        assert rows[0]["review_state"] == "uncertain"
        assert rows[0]["evidence_ids"] == ["source-evidence"]
        with patch(
            "pandrator.logic.llm_handler.chat_completion_with_metadata",
            return_value='[{"cue_id":1,"text":"Ein vollständiger Gedanke aus beiden Teilsätzen."},{"cue_id":2,"text":"Ein abschließender Gedanke."}]',
        ) as completion:
            translation = handlers.translate(
                {"session_id": session_id, "source_artifact_id": corrected.id},
                lambda *_args: None,
                Event(),
            )
        assert completion.call_count == 1
        translated, path = case.extension["artifacts"].resolve(
            translation["artifact_id"]
        )
        translated_rows = stored_passages(translated)
        assert [(row["start_ms"], row["end_ms"]) for row in translated_rows] == [
            (0, 13680),
            (14000, 19000),
        ]
        assert translated_rows[0]["evidence_ids"] == ["source-evidence"]
        with case.extension["database"].session() as session:
            display = list(
                session.scalars(
                    select(Segment)
                    .where(
                        Segment.revision_id == translated.metadata_json["revision_id"]
                    )
                    .order_by(Segment.ordinal)
                )
            )
            assert display[0].metadata_json["review_state"] == "uncertain"
        records, _, _ = handlers._subtitle_generation_records(
            translated, path, {}, "de"
        )
        assert records


def test_changed_source_file_does_not_silently_use_saved_passages(app_case):
    case = app_case
    _, original, path = source(case)
    with case.extension["database"].session() as session:
        managed = session.get(Artifact, original.id)
        attach_passages(managed, source_passages(session, managed), source=managed)
    path.write_text(
        path.read_text().replace("meaningful", "different"), encoding="utf-8"
    )
    handlers = case.extension["workflow_handlers"]
    with pytest.raises(ValueError, match="subtitle file changed"):
        handlers._prepare_passage_input(original, path, path.parent)
    with pytest.raises(ValueError, match="subtitle file changed"):
        handlers._subtitle_generation_records(original, path, {}, "en")


def test_translation_schemas_reject_ambiguous_and_boolean_ids():
    from pydantic import ValidationError

    from pandrator.web.schemas import DispatchTranslationItem
    from pandrator_mcp.schemas.dispatch import DispatchTranslationItemInput

    for schema in (DispatchTranslationItem, DispatchTranslationItemInput):
        assert schema.model_validate(
            {"cue_ids": [1, 2], "text": "Merged."}
        ).cue_ids == [1, 2]
        for invalid in (
            {"cue_id": True},
            {"cue_ids": [True]},
            {"cue_id": 1, "cue_ids": [1]},
            {},
        ):
            with pytest.raises(ValidationError):
                schema.model_validate({**invalid, "text": "Invalid."})


def test_overlapping_passages_keep_uncertainty_and_audio_evidence_ownership(app_case):
    from pandrator.web.models import SubtitleEvidence

    case = app_case
    session_id, original, _ = source(case)
    rows = [
        {
            "id": "first",
            "start_ms": 0,
            "end_ms": 2000,
            "text": "First speaker.",
            "speaker": "SPEAKER_00",
        },
        {
            "id": "second",
            "start_ms": 1000,
            "end_ms": 3000,
            "text": "Second speaker.",
            "speaker": "SPEAKER_01",
        },
    ]
    overlapping, _ = register_rows(
        case, session_id, rows, role="correction", parent=original
    )
    with case.extension["database"].session() as session:
        second = session.scalar(
            select(Segment).where(
                Segment.revision_id == overlapping.metadata_json["revision_id"],
                Segment.ordinal == 1,
            )
        )
        second.metadata_json = {
            "review_state": "uncertain",
            "review_note": "Only this speaker is unclear.",
            "evidence_ids": [],
            "uncertain_source_cue_ids": [2],
        }
        evidence = SubtitleEvidence(
            session_id=session_id,
            source_artifact_id=overlapping.id,
            source_revision_id=overlapping.metadata_json["revision_id"],
            source_segment_id=second.id,
            cue_id=2,
            start_ms=1000,
            end_ms=3000,
            clip_start_ms=1000,
            clip_end_ms=3000,
            reason="Unclear word.",
            status="completed",
        )
        session.add(evidence)
        session.flush()
        evidence_id = evidence.id
        prepared = source_passages(session, session.get(Artifact, overlapping.id))
        assert prepared[0]["review_state"] == "clear"
        assert prepared[1]["review_state"] == "uncertain"
    claim = case._claim(
        case._create(session_id, source_artifact_id=overlapping.id)["id"]
    )
    assert [cue["evidence_cue_ids"] for cue in claim["batch"]["cues"]] == [[1], [2]]
    corrected, _ = submit(
        case,
        claim,
        {
            "kind": "correction",
            "operations": [
                {"action": "edit", "cue_ids": [1], "texts": ["The first speaker."]}
            ],
            "uncertainties": [
                {
                    "cue_id": 2,
                    "reason": "Only the second speaker remains unclear.",
                    "evidence_ids": [evidence_id],
                }
            ],
        },
    )
    result = stored_passages(corrected)
    assert result[0]["review_state"] == "clear"
    assert result[0]["evidence_ids"] == []
    assert result[1]["review_state"] == "uncertain"
    assert result[1]["evidence_ids"] == [evidence_id]


@pytest.mark.parametrize("merge_stage", ["correction", "translation"])
def test_unfinished_pause_merge_survives_real_dispatch_routes(app_case, merge_stage):
    case = app_case
    session_id, _, _ = source(case)
    input_rows = [
        {"text": "A local religious", "start_ms": 0, "end_ms": 2400, "speaker": "SPEAKER_00"},
        {"text": "controversy becomes a national issue.", "start_ms": 4560, "end_ms": 9160, "speaker": "SPEAKER_00"},
        {"text": "An independent final thought.", "start_ms": 9360, "end_ms": 12000, "speaker": "SPEAKER_00"},
    ]
    original, _ = register_rows(case, session_id, input_rows, name="unfinished-pause.srt")
    correction = case._create(session_id, source_artifact_id=original.id)
    claim = case._claim(correction["id"])
    operations = (
        [{"action": "merge", "cue_ids": [1, 2], "texts": ["A local religious controversy becomes a national issue."]}]
        if merge_stage == "correction"
        else [{"action": "edit", "cue_ids": [1], "texts": ["A local religious"]}]
    )
    corrected, _ = submit(case, claim, {"kind": "correction", "operations": operations})
    translation = case._create(session_id, kind="translation", source_artifact_id=corrected.id)
    translated_claim = case._claim(translation["id"], key="claim-pause-translation")
    first = {"text": "Eine lokale religiöse Kontroverse wird zu einer nationalen Angelegenheit."}
    first.update({"cue_id": 1} if merge_stage == "correction" else {"cue_ids": [1, 2]})
    translated, path = submit(case, translated_claim, {
        "kind": "translation", "translations": [
            first, {"cue_id": 2 if merge_stage == "correction" else 3,
                    "text": "Ein unabhängiger letzter Gedanke."},
        ],
    })
    passages = stored_passages(translated)
    assert len(passages) == 2
    assert (passages[0]["start_ms"], passages[0]["end_ms"]) == (0, 9160)
    assert passages[0]["text"] == first["text"]
    records, _, _ = case.extension["workflow_handlers"]._subtitle_generation_records(
        translated, path, {"speech_block_max_chars": 300}, "de"
    )
    cues = [cue for block in records for cue in block["provenance"]["source_cues"]]
    assert [(cue["start_ms"], cue["end_ms"]) for cue in cues] == [(0, 9160), (9360, 12000)]
    assert all(cue["start_ms"] != 4560 for cue in cues)


@pytest.mark.parametrize("confidence,expected_count", [(0.1, 1), (0.9, 2)])
def test_word_confidence_reaches_source_boundary_builder(app_case, confidence, expected_count):
    case = app_case
    _, source_id = case._source(texts=("First sentence. Second sentence.",))
    with case.extension["database"].session() as session:
        artifact = session.get(Artifact, source_id)
        cue = session.scalar(select(Segment).where(
            Segment.revision_id == artifact.metadata_json["revision_id"]))
        for index, text in enumerate(cue.text.split()):
            session.add(TimedWord(revision_id=cue.revision_id, segment_id=cue.id,
                                  ordinal=index, text=text, start_ms=index * 250,
                                  end_ms=(index + 1) * 250,
                                  confidence=confidence if index == 1 else 1.0))
    with case.extension["database"].session() as session:
        result = source_passages(session, session.get(Artifact, source_id))
        assert len(result) == expected_count
        assert ' '.join(p['text'] for p in result) == "First sentence. Second sentence."


def test_preserved_ledger_is_not_resegmented_by_new_policy(app_case):
    _, original, _ = source(app_case)
    with app_case.extension["database"].session() as session:
        managed = session.get(Artifact, original.id)
        rows = source_passages(session, managed)
        attach_passages(managed, rows, source=managed)
        with patch('pandrator.web.logical_passages.build_source_passages',
                   side_effect=AssertionError('Saved passages must stay saved')):
            assert source_passages(session, managed) == rows
