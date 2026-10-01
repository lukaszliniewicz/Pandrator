"""Review saves preserve selected logical utterances and verified split timing."""

from __future__ import annotations

from unittest.mock import patch

import pytest
from sqlalchemy import select

from pandrator.web.logical_passages import (
    attach_passages,
    map_output_passages,
    materialize_speech_source,
    source_passages,
    stored_passages,
)
from pandrator.web.models import (
    Artifact,
    Document,
    DocumentRevision,
    Segment,
    SessionRecord,
    SubtitleEvidence,
    TimedWord,
)
from tests import test_web_subtitle_review as legacy_review_tests


@pytest.fixture
def review_case():
    case = legacy_review_tests.SubtitleReviewTests(methodName="runTest")
    case.setUp()
    try:
        yield case
    finally:
        case.tearDown()


def _source(case, role="correction", *, same_turn=False):
    artifact = case._artifact(
        f"turn-source-{role}.srt",
        role,
        "1\n00:00:00,000 --> 00:00:01,000\nFirst.\n\n2\n00:00:01,000 --> 00:00:02,000\nSecond.\n",
    )
    with case.database.session() as session:
        managed = session.get(Artifact, artifact.id)
        segments = list(
            session.scalars(
                select(Segment)
                .where(Segment.revision_id == managed.metadata_json["revision_id"])
                .order_by(Segment.ordinal)
            )
        )
        rows = [
            {
                "id": f"p{index + 1:06d}",
                "text": item.text,
                "start_ms": item.start_ms,
                "end_ms": item.end_ms,
                "speaker": "",
                "turn_id": "named-turn-a" if same_turn or index == 0 else "named-turn-b",
                "source_cue_ids": [item.id],
            }
            for index, item in enumerate(segments)
        ]
        attach_passages(managed, rows, source=managed)
        return managed.id, managed.content_hash, segments[0].id, segments[1].id


@pytest.mark.parametrize("stage", ["correction", "translation"])
def test_plain_review_retains_turn_packet_for_speech(review_case, stage):
    source_id, source_hash, first_id, second_id = _source(review_case, stage)
    selected = review_case.service.review(review_case.session.id, [source_id])["columns"][0]
    assert [item["turn_id"] for item in selected["segments"]] == ["named-turn-a", "named-turn-b"]
    assert [item["source_passage_ids"] for item in selected["segments"]] == [
        ["p000001"],
        ["p000002"],
    ]
    result = review_case.service.save_review(
        review_case.session.id,
        stage,
        selected["revision"],
        [
            {
                "id": first_id,
                "turn_id": "named-turn-a",
                "source_passage_ids": ["p000001"],
                "start_ms": 0,
                "end_ms": 1000,
                "text": "First edited.",
            },
            {
                "id": second_id,
                "turn_id": "named-turn-b",
                "source_passage_ids": ["p000002"],
                "start_ms": 1000,
                "end_ms": 2000,
                "text": "Second.",
            },
        ],
        source_artifact_id=source_id,
        expected_source_hash=source_hash,
    )
    with review_case.database.session() as session:
        artifact = session.get(Artifact, result["artifact_id"])
        rows = stored_passages(artifact)
        assert [row["turn_id"] for row in rows] == ["named-turn-a", "named-turn-b"]
        assert [row["source_passage_ids"] for row in rows] == [["p000001"], ["p000002"]]
        translation_inputs = source_passages(session, artifact)
        assert [row["turn_id"] for row in translation_inputs] == ["named-turn-a", "named-turn-b"]
        with pytest.raises(ValueError, match="turn boundary"):
            map_output_passages(
                [
                    {
                        "source_cue_ids": [1, 2],
                        "text": "Merged translation",
                        "start_ms": 0,
                        "end_ms": 2000,
                    }
                ],
                translation_inputs,
            )
        speech = materialize_speech_source(session, artifact)
        assert speech is not None
        assert [row["turn_id"] for row in speech[0]] == ["named-turn-a", "named-turn-b"]


def test_cross_turn_merge_rejected_and_same_turn_merge_kept(review_case):
    source_id, source_hash, _, _ = _source(review_case)
    with pytest.raises(ValueError, match="turn boundary"):
        review_case.service.save_review(
            review_case.session.id,
            "correction",
            1,
            [{"start_ms": 0, "end_ms": 2000, "text": "Together."}],
            source_artifact_id=source_id,
            expected_source_hash=source_hash,
        )
    with review_case.database.session() as session:
        managed = session.get(Artifact, source_id)
        rows = list(stored_passages(managed))
        rows[1]["turn_id"] = rows[0]["turn_id"]
        attach_passages(managed, rows, source=managed)
    result = review_case.service.save_review(
        review_case.session.id,
        "correction",
        1,
        [{"start_ms": 0, "end_ms": 2000, "text": "Together."}],
        source_artifact_id=source_id,
        expected_source_hash=source_hash,
    )
    with review_case.database.session() as session:
        rows = stored_passages(session.get(Artifact, result["artifact_id"]))
        assert len(rows) == 1
        assert rows[0]["turn_id"] == "named-turn-a"
        assert rows[0]["source_passage_ids"] == ["p000001", "p000002"]


def test_split_requires_verified_source_word_anchor(review_case):
    artifact = review_case._artifact(
        "split-source.srt",
        "correction",
        "1\n00:00:00,000 --> 00:00:04,000\nLuke yes thank you\n",
    )
    with review_case.database.session() as session:
        managed = session.get(Artifact, artifact.id)
        session.get(SessionRecord, review_case.session.id).source_language = "en"
        session.get(Document, managed.metadata_json["document_id"]).language = "en"
        segment = session.scalar(
            select(Segment).where(Segment.revision_id == managed.metadata_json["revision_id"])
        )
        words = []
        for ordinal, (word, start, end) in enumerate(
            [
                ("Luke", 0, 800),
                ("yes", 1000, 1600),
                ("thank", 1800, 2500),
                ("you", 2700, 4000),
            ]
        ):
            timed = TimedWord(
                revision_id=segment.revision_id,
                segment_id=segment.id,
                ordinal=ordinal,
                text=word,
                start_ms=start,
                end_ms=end,
            )
            session.add(timed)
            session.flush()
            words.append(timed.id)
        attach_passages(
            managed,
            [
                {
                    "id": "p000001",
                    "text": "Luke yes thank you",
                    "start_ms": 0,
                    "end_ms": 4000,
                    "speaker": "",
                    "turn_id": "named-turn-a",
                    "source_cue_ids": [segment.id],
                    "source_word_ids": words,
                }
            ],
            source=managed,
        )
        source_hash, segment_id = managed.content_hash, segment.id
    inspected = review_case.service.inspect_review_split_boundaries(
        review_case.session.id, artifact.id, segment_id, 1
    )
    assert inspected["status"] == "available"
    anchor = inspected["boundaries"][0]
    values = [
        {
            "origin_segment_id": segment_id,
            "split_boundary_id": anchor["id"],
            "start_ms": 0,
            "end_ms": anchor["left_end_ms"],
            "text": "Luke",
        },
        {
            "origin_segment_id": segment_id,
            "split_boundary_id": anchor["id"],
            "start_ms": anchor["right_start_ms"],
            "end_ms": 4000,
            "text": "Yes thank you",
        },
    ]
    with pytest.raises(ValueError, match="unavailable or stale"):
        review_case.service.save_review(
            review_case.session.id,
            "correction",
            1,
            [{**item, "split_boundary_id": "forged"} for item in values],
            source_artifact_id=artifact.id,
            expected_source_hash=source_hash,
        )
    with pytest.raises(ValueError, match="verified source-word windows"):
        review_case.service.save_review(
            review_case.session.id,
            "correction",
            1,
            [{**values[0], "end_ms": anchor["left_end_ms"] + 1}, values[1]],
            source_artifact_id=artifact.id,
            expected_source_hash=source_hash,
        )
    result = review_case.service.save_review(
        review_case.session.id,
        "correction",
        1,
        values,
        source_artifact_id=artifact.id,
        expected_source_hash=source_hash,
    )
    with review_case.database.session() as session:
        rows = stored_passages(session.get(Artifact, result["artifact_id"]))
        assert [(row["start_ms"], row["end_ms"]) for row in rows] == [(0, 800), (1000, 4000)]
        assert [row["source_word_ids"] for row in rows] == [words[:1], words[1:]]


def test_overlapping_split_rejects_foreign_child_identity_and_passage(review_case):
    artifact = review_case._artifact(
        "overlapping-split-source.srt",
        "correction",
        "1\n00:00:00,000 --> 00:00:04,000\nLuke yes thank you\n\n"
        "2\n00:00:01,000 --> 00:00:03,000\nOther speaker\n",
    )
    with review_case.database.session() as session:
        managed = session.get(Artifact, artifact.id)
        session.get(SessionRecord, review_case.session.id).source_language = "en"
        session.get(Document, managed.metadata_json["document_id"]).language = "en"
        segments = list(
            session.scalars(
                select(Segment)
                .where(Segment.revision_id == managed.metadata_json["revision_id"])
                .order_by(Segment.ordinal)
            )
        )
        first, second = segments
        word_ids = []
        for ordinal, (word, start, end) in enumerate(
            [
                ("Luke", 0, 800),
                ("yes", 1000, 1600),
                ("thank", 1800, 2500),
                ("you", 2700, 4000),
            ]
        ):
            timed = TimedWord(
                revision_id=first.revision_id,
                segment_id=first.id,
                ordinal=ordinal,
                text=word,
                start_ms=start,
                end_ms=end,
            )
            session.add(timed)
            session.flush()
            word_ids.append(timed.id)
        attach_passages(
            managed,
            [
                {
                    "id": "passage-a",
                    "text": "Luke yes thank you",
                    "start_ms": 0,
                    "end_ms": 4000,
                    "speaker": "",
                    "turn_id": "turn-a",
                    "source_cue_ids": [first.id],
                    "source_word_ids": word_ids,
                },
                {
                    "id": "passage-b",
                    "text": "Other speaker",
                    "start_ms": 1000,
                    "end_ms": 3000,
                    "speaker": "",
                    "turn_id": "turn-b",
                    "source_cue_ids": [second.id],
                },
            ],
            source=managed,
        )
        source_hash, first_id, second_id = managed.content_hash, first.id, second.id
    inspected = review_case.service.inspect_review_split_boundaries(
        review_case.session.id, artifact.id, first_id, 1
    )
    assert inspected["status"] == "available"
    anchor = inspected["boundaries"][0]
    children = [
        {
            "origin_segment_id": first_id,
            "split_boundary_id": anchor["id"],
            "source_passage_ids": ["passage-a"],
            "start_ms": 0,
            "end_ms": anchor["left_end_ms"],
            "text": "Luke",
        },
        {
            "origin_segment_id": first_id,
            "split_boundary_id": anchor["id"],
            "source_passage_ids": ["passage-a"],
            "start_ms": anchor["right_start_ms"],
            "end_ms": 4000,
            "text": "Yes thank you",
        },
    ]
    other = {
        "id": second_id,
        "source_passage_ids": ["passage-b"],
        "start_ms": 1000,
        "end_ms": 3000,
        "text": "Other speaker",
    }
    with pytest.raises(ValueError, match="Split child id"):
        review_case.service.save_review(
            review_case.session.id,
            "correction",
            1,
            [{**children[0], "id": second_id}, children[1], other],
            source_artifact_id=artifact.id,
            expected_source_hash=source_hash,
        )
    with pytest.raises(ValueError, match="Split child logical source references"):
        review_case.service.save_review(
            review_case.session.id,
            "correction",
            1,
            [{**children[0], "source_passage_ids": ["passage-b"]}, children[1], other],
            source_artifact_id=artifact.id,
            expected_source_hash=source_hash,
        )
    result = review_case.service.save_review(
        review_case.session.id,
        "correction",
        1,
        [*children, other],
        source_artifact_id=artifact.id,
        expected_source_hash=source_hash,
    )
    with review_case.database.session() as session:
        rows = stored_passages(session.get(Artifact, result["artifact_id"]))
        assert [row["source_passage_ids"] for row in rows] == [
            ["passage-a"],
            ["passage-a"],
            ["passage-b"],
        ]
        assert [row["turn_id"] for row in rows] == ["turn-a", "turn-a", "turn-b"]
        assert rows[0]["source_word_ids"] == word_ids[:1]
        assert rows[1]["source_word_ids"] == word_ids[1:]


def test_revision_and_source_hash_conflicts(review_case):
    source_id, source_hash, _, _ = _source(review_case)
    value = [{"start_ms": 0, "end_ms": 1000, "text": "First."}]
    with pytest.raises(RuntimeError, match="revision"):
        review_case.service.save_review(
            review_case.session.id,
            "correction",
            999,
            value,
            source_artifact_id=source_id,
            expected_source_hash=source_hash,
        )
    with pytest.raises(RuntimeError, match="hash"):
        review_case.service.save_review(
            review_case.session.id,
            "correction",
            1,
            value,
            source_artifact_id=source_id,
            expected_source_hash="forged",
        )


def test_forged_turn_and_segment_rejected_and_explicit_new_turn_kept(review_case):
    source_id, source_hash, first_id, second_id = _source(review_case, same_turn=True)
    selected = review_case.service.review(review_case.session.id, [source_id])["columns"][0]
    original = [{**item, "text": item["text"]} for item in selected["segments"]]
    with pytest.raises(ValueError, match="turn_id"):
        review_case.service.save_review(
            review_case.session.id,
            "correction",
            1,
            [{**original[0], "turn_id": "forged"}, original[1]],
            source_artifact_id=source_id,
            expected_source_hash=source_hash,
        )
    with pytest.raises(ValueError, match="identity"):
        review_case.service.save_review(
            review_case.session.id,
            "correction",
            1,
            [{**original[0], "id": "forged"}, original[1]],
            source_artifact_id=source_id,
            expected_source_hash=source_hash,
        )
    with pytest.raises(ValueError, match="Logical source references"):
        review_case.service.save_review(
            review_case.session.id,
            "correction",
            1,
            [{**original[0], "source_passage_ids": ["p000002"]}, original[1]],
            source_artifact_id=source_id,
            expected_source_hash=source_hash,
        )
    result = review_case.service.save_review(
        review_case.session.id,
        "correction",
        1,
        [original[0], {**original[1], "starts_new_turn": True}],
        source_artifact_id=source_id,
        expected_source_hash=source_hash,
    )
    with review_case.database.session() as session:
        rows = stored_passages(session.get(Artifact, result["artifact_id"]))
        assert rows[0]["turn_id"] == "named-turn-a"
        assert rows[1]["turn_id"].startswith("turn-")
        assert rows[1]["turn_id"] != rows[0]["turn_id"]


def test_no_word_anchor_split_rejected_and_review_evidence_preserved(review_case):
    source_id, source_hash, first_id, second_id = _source(review_case)
    inspected = review_case.service.inspect_review_split_boundaries(
        review_case.session.id, source_id, first_id, 1
    )
    assert inspected["status"] == "unavailable"
    assert inspected["reason"]
    with pytest.raises(ValueError, match="unavailable or stale"):
        review_case.service.save_review(
            review_case.session.id,
            "correction",
            1,
            [
                {
                    "origin_segment_id": first_id,
                    "split_boundary_id": "forged",
                    "start_ms": 0,
                    "end_ms": 400,
                    "text": "First",
                },
                {
                    "origin_segment_id": first_id,
                    "split_boundary_id": "forged",
                    "start_ms": 500,
                    "end_ms": 1000,
                    "text": ".",
                },
            ],
            source_artifact_id=source_id,
            expected_source_hash=source_hash,
        )
    with review_case.database.session() as session:
        session.add(
            SubtitleEvidence(
                id="review-turn-evidence",
                session_id=review_case.session.id,
                source_artifact_id=source_id,
                source_revision_id=session.get(Artifact, source_id).metadata_json["revision_id"],
                source_segment_id=first_id,
                cue_id=1,
                start_ms=0,
                end_ms=1000,
                clip_start_ms=0,
                clip_end_ms=1000,
                reason="Unclear name",
                routes_json=["whisper"],
                audio_model_ids_json=[],
                status="completed",
                candidates_json=[],
                resolution_json={},
            )
        )
    result = review_case.service.save_review(
        review_case.session.id,
        "correction",
        1,
        [
            {
                "id": first_id,
                "start_ms": 0,
                "end_ms": 1000,
                "text": "First.",
                "review_state": "uncertain",
                "review_note": "Check name",
                "evidence_ids": ["review-turn-evidence"],
                "uncertain_source_cue_ids": [1],
            },
            {"id": second_id, "start_ms": 1000, "end_ms": 2000, "text": "Second."},
        ],
        source_artifact_id=source_id,
        expected_source_hash=source_hash,
    )
    with review_case.database.session() as session:
        rows = stored_passages(session.get(Artifact, result["artifact_id"]))
        assert rows[0]["review_state"] == "uncertain"
        assert rows[0]["review_note"] == "Check name"
        assert rows[0]["evidence_ids"] == ["review-turn-evidence"]
        assert rows[0]["uncertain_source_cue_ids"] == [1]


@pytest.mark.parametrize("edit_text", [False, True])
def test_display_fragments_reconstruct_one_logical_utterance_and_review_metadata(
    review_case, edit_text
):
    source_id, source_hash, first_id, second_id = _source(review_case, same_turn=True)
    with review_case.database.session() as session:
        managed = session.get(Artifact, source_id)
        attach_passages(
            managed,
            [
                {
                    "id": "one-utterance",
                    "text": "First. Second.",
                    "start_ms": 0,
                    "end_ms": 2000,
                    "speaker": "",
                    "turn_id": "named-turn-a",
                    "source_cue_ids": [first_id, second_id],
                }
            ],
            source=managed,
        )
        session.add(
            SubtitleEvidence(
                id="fragment-evidence",
                session_id=review_case.session.id,
                source_artifact_id=source_id,
                source_revision_id=managed.metadata_json["revision_id"],
                source_segment_id=first_id,
                cue_id=1,
                start_ms=0,
                end_ms=1000,
                clip_start_ms=0,
                clip_end_ms=1000,
                reason="Check first fragment",
                routes_json=["whisper"],
                audio_model_ids_json=[],
                status="completed",
                candidates_json=[],
                resolution_json={},
            )
        )
    selected = review_case.service.review(review_case.session.id, [source_id])["columns"][0]
    values = [
        {
            key: item[key]
            for key in (
                "id",
                "start_ms",
                "end_ms",
                "text",
                "speaker",
                "turn_id",
                "source_passage_ids",
            )
        }
        for item in selected["segments"]
    ]
    values[0].update(
        review_state="uncertain",
        review_note="Check first fragment",
        evidence_ids=["fragment-evidence"],
        uncertain_source_cue_ids=[1],
    )
    if edit_text:
        values[1]["text"] = "Second edited."
    result = review_case.service.save_review(
        review_case.session.id,
        "correction",
        1,
        values,
        source_artifact_id=source_id,
        expected_source_hash=source_hash,
    )
    with review_case.database.session() as session:
        rows = stored_passages(session.get(Artifact, result["artifact_id"]))
        assert len(rows) == 1
        assert rows[0]["id"] == "one-utterance"
        assert rows[0]["turn_id"] == "named-turn-a"
        assert rows[0]["text"] == ("First. Second edited." if edit_text else "First. Second.")
        assert rows[0]["review_state"] == "uncertain"
        assert rows[0]["review_note"] == "Check first fragment"
        assert rows[0]["evidence_ids"] == ["fragment-evidence"]
        assert rows[0]["uncertain_source_cue_ids"] == [1]


def test_display_fragment_forgery_or_omission_rejected(review_case):
    source_id, source_hash, first_id, second_id = _source(review_case, same_turn=True)
    with review_case.database.session() as session:
        managed = session.get(Artifact, source_id)
        attach_passages(
            managed,
            [
                {
                    "id": "one-utterance",
                    "text": "First. Second.",
                    "start_ms": 0,
                    "end_ms": 2000,
                    "speaker": "",
                    "turn_id": "named-turn-a",
                    "source_cue_ids": [first_id, second_id],
                }
            ],
            source=managed,
        )
    selected = review_case.service.review(review_case.session.id, [source_id])["columns"][0]
    values = [
        {
            key: item[key]
            for key in (
                "id",
                "start_ms",
                "end_ms",
                "text",
                "speaker",
                "turn_id",
                "source_passage_ids",
            )
        }
        for item in selected["segments"]
    ]
    values[0]["text"] = "First edited."
    with pytest.raises(ValueError, match="every inherited source segment"):
        review_case.service.save_review(
            review_case.session.id,
            "correction",
            1,
            values[:1],
            source_artifact_id=source_id,
            expected_source_hash=source_hash,
        )
    with pytest.raises(ValueError, match="exact source segment and logical references"):
        review_case.service.save_review(
            review_case.session.id,
            "correction",
            1,
            [{**values[0], "source_passage_ids": []}, values[1]],
            source_artifact_id=source_id,
            expected_source_hash=source_hash,
        )
    with pytest.raises(ValueError, match="identity"):
        review_case.service.save_review(
            review_case.session.id,
            "correction",
            1,
            [{**values[0], "id": "forged"}, values[1]],
            source_artifact_id=source_id,
            expected_source_hash=source_hash,
        )


def test_registration_failure_rolls_back_revision_and_file(review_case):
    source_id, source_hash, first_id, second_id = _source(review_case)
    selected = review_case.service.review(review_case.session.id, [source_id])["columns"][0]
    destination = review_case.session_dir / "reviewed_correction_r2.srt"
    with patch.object(
        review_case.artifacts, "register_in_session", side_effect=RuntimeError("register failed")
    ):
        with pytest.raises(RuntimeError, match="register failed"):
            review_case.service.save_review(
                review_case.session.id,
                "correction",
                1,
                [
                    {"id": first_id, "start_ms": 0, "end_ms": 1000, "text": "First edited."},
                    {"id": second_id, "start_ms": 1000, "end_ms": 2000, "text": "Second."},
                ],
                source_artifact_id=source_id,
                expected_source_hash=source_hash,
            )
    assert not destination.exists()
    with review_case.database.session() as session:
        revisions = list(
            session.scalars(
                select(DocumentRevision).where(
                    DocumentRevision.document_id == selected["document_id"]
                )
            )
        )
        assert [revision.revision_number for revision in revisions] == [1]
