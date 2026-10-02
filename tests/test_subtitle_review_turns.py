"""Review saves preserve selected logical utterances and verified split timing."""

from __future__ import annotations

from unittest.mock import patch

import pytest
from sqlalchemy import select

from pandrator.web.document_roles import document_stage_for_artifact_role
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


def test_artifact_role_stage_alias_preserves_same_name_document_stages():
    assert document_stage_for_artifact_role("tts_optimized") == "tts_optimization"
    assert document_stage_for_artifact_role("media_edit_subtitles") == "media_edit_subtitles"
    assert document_stage_for_artifact_role(None) is None


def test_review_payload_marks_exact_ownership_and_returns_canonical_passages(
    review_case,
):
    source_id, _source_hash, _first_id, _second_id = _source(review_case)
    column = review_case.service.review(review_case.session.id, [source_id])["columns"][0]
    cue = column["segments"][0]
    capabilities = cue["edit_capabilities"]
    assert capabilities == {
        "ownership": "exact",
        "text": True,
        "display_timing": True,
        "speaker": True,
        "start_new_utterance": True,
        "split": True,
        "merge": True,
        "delete": True,
        "reason": "",
    }
    assert set(cue["owned_passages"][0]) == {
        "id",
        "text",
        "speaker",
        "start_ms",
        "end_ms",
        "turn_id",
        "review_state",
        "review_note",
    }
    with review_case.database.session() as session:
        artifact = session.get(Artifact, source_id)
        assert column["logical_passages"] == stored_passages(artifact)


def test_review_payload_rebuilds_missing_passages_through_guarded_source_reader(
    review_case,
):
    source_id, _source_hash, _first_id, _second_id = _source(review_case)
    with review_case.database.session() as session:
        artifact = session.get(Artifact, source_id)
        metadata = dict(artifact.metadata_json)
        packet = dict(metadata["logical_passages"])
        packet["display_content_hash"] = "stale-content-hash"
        packet["items"] = [
            {
                "id": "stale-passage",
                "text": "Stale packet data.",
                "start_ms": 0,
                "end_ms": 2000,
                "speaker": "STALE",
            }
        ]
        artifact.metadata_json = {**metadata, "logical_passages": packet}
    column = review_case.service.review(review_case.session.id, [source_id])["columns"][0]
    assert [row["id"] for row in column["logical_passages"]] != ["stale-passage"]
    assert [row["text"] for row in column["logical_passages"]] == ["First.", "Second."]
    assert all(
        cue["edit_capabilities"]["ownership"] == "exact"
        for cue in column["segments"]
    )


def test_review_payload_without_owned_passages_is_unverified(review_case):
    _source_id, _source_hash, first_id, _second_id = _source(review_case)
    with review_case.database.session() as session:
        segment = session.get(Segment, first_id)
        payload = review_case.service._payload_with_passages(segment, [])
    assert payload["owned_passages"] == []
    assert payload["edit_capabilities"] == {
        "ownership": "unverified",
        "text": False,
        "display_timing": False,
        "speaker": False,
        "start_new_utterance": False,
        "split": False,
        "merge": False,
        "delete": False,
        "reason": "Capabilities are unknown because this display cue has no verified source passage.",
    }


def test_tts_optimized_review_can_save_and_reopen_unchanged(review_case):
    first = review_case.service.save_review(
        review_case.session.id,
        "tts_optimization",
        0,
        [{"start_ms": 0, "end_ms": 1000, "text": "Optimized line."}],
    )
    selected = review_case.service.review(
        review_case.session.id, [first["artifact_id"]]
    )["columns"][0]
    assert selected["role"] == "tts_optimized"
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
    saved = review_case.service.save_review(
        review_case.session.id,
        "tts_optimization",
        selected["revision"],
        values,
        source_artifact_id=first["artifact_id"],
        expected_source_hash=selected["source_content_hash"],
    )
    reopened = review_case.service.review(
        review_case.session.id, [saved["artifact_id"]]
    )["columns"][0]
    assert reopened["role"] == "tts_optimized"
    assert [item["text"] for item in reopened["segments"]] == ["Optimized line."]
    with review_case.database.session() as session:
        artifact = session.get(Artifact, saved["artifact_id"])
        assert [row["text"] for row in source_passages(session, artifact)] == [
            "Optimized line."
        ]


def test_tts_optimized_source_passages_keep_ownership_revision_and_delete_guards(
    review_case,
):
    created = review_case.service.save_review(
        review_case.session.id,
        "tts_optimization",
        0,
        [{"start_ms": 0, "end_ms": 1000, "text": "Optimized line."}],
    )
    other_session = review_case.sessions.create(
        "Other source owner", workflow_kind="subtitles"
    )
    with review_case.database.session() as session:
        artifact = session.get(Artifact, created["artifact_id"])
        metadata = dict(artifact.metadata_json)
        state = artifact.state
        assert source_passages(session, artifact)

        document = session.get(Document, metadata["document_id"])
        owner = document.session_id
        document.session_id = other_session.id
        assert source_passages(session, artifact) == []
        document.session_id = owner

        artifact.metadata_json = {**metadata, "revision_id": "missing-revision"}
        assert source_passages(session, artifact) == []
        artifact.metadata_json = metadata

        artifact.state = "deleted"
        assert source_passages(session, artifact) == []
        artifact.state = state


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
    assert [
        item["edit_capabilities"]["ownership"] for item in selected["segments"]
    ] == ["fragment", "fragment"]
    for item in selected["segments"]:
        assert item["edit_capabilities"] == {
            "ownership": "fragment",
            "text": True,
            "display_timing": False,
            "speaker": False,
            "start_new_utterance": False,
            "split": False,
            "merge": False,
            "delete": False,
            "reason": (
                "This display cue is part of spoken passage one-utterance. Change its "
                "speaker, timing or utterance at passage level; keep every fragment "
                "when saving."
            ),
        }
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
        assert rows[0]["id"] == "p000001"
        assert rows[0]["turn_id"] == "named-turn-a"
        assert rows[0]["text"] == ("First. Second edited." if edit_text else "First. Second.")
        assert rows[0]["review_state"] == "uncertain"
        assert rows[0]["review_note"] == "Check first fragment"
        assert rows[0]["evidence_ids"] == ["fragment-evidence"]
        assert rows[0]["uncertain_source_cue_ids"] == [1]


def test_display_fragment_speaker_edit_updates_canonical_and_materialized_speech(
    review_case,
):
    source_id, source_hash, _first_id, _second_id = _source(
        review_case, same_turn=True
    )
    with review_case.database.session() as session:
        source = session.get(Artifact, source_id)
        segments = list(
            session.scalars(
                select(Segment)
                .where(Segment.revision_id == source.metadata_json["revision_id"])
                .order_by(Segment.ordinal)
            )
        )
        attach_passages(
            source,
            [
                {
                    "id": "one-utterance",
                    "text": "First. Second.",
                    "start_ms": 0,
                    "end_ms": 2000,
                    "speaker": "",
                    "turn_id": "named-turn-a",
                    "source_cue_ids": [item.id for item in segments],
                }
            ],
            source=source,
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
    for item in values:
        item["speaker"] = "NARRATOR"
    saved = review_case.service.save_review(
        review_case.session.id,
        "correction",
        1,
        values,
        source_artifact_id=source_id,
        expected_source_hash=source_hash,
    )
    with review_case.database.session() as session:
        artifact = session.get(Artifact, saved["artifact_id"])
        rows = stored_passages(artifact)
        assert rows[0]["speaker"] == "NARRATOR"
        speech = materialize_speech_source(session, artifact)
        assert speech is not None
        speech_rows, speech_revision_id = speech
        assert speech_rows[0]["speaker"] == "NARRATOR"
        materialized = list(
            session.scalars(
                select(Segment)
                .where(Segment.revision_id == speech_revision_id)
                .order_by(Segment.ordinal)
            )
        )
        assert [item.speaker for item in materialized] == ["NARRATOR"]


def test_unchanged_fragments_preserve_canonical_speaker_when_display_is_blank(
    review_case,
):
    source_id, source_hash, _first_id, _second_id = _source(
        review_case, same_turn=True
    )
    with review_case.database.session() as session:
        source = session.get(Artifact, source_id)
        segments = list(
            session.scalars(
                select(Segment)
                .where(Segment.revision_id == source.metadata_json["revision_id"])
                .order_by(Segment.ordinal)
            )
        )
        attach_passages(
            source,
            [
                {
                    "id": "one-utterance",
                    "text": "First. Second.",
                    "start_ms": 0,
                    "end_ms": 2000,
                    "speaker": "NARRATOR",
                    "turn_id": "named-turn-a",
                    "source_cue_ids": [item.id for item in segments],
                }
            ],
            source=source,
        )
        assert [item.speaker or "" for item in segments] == ["", ""]
    selected = review_case.service.review(review_case.session.id, [source_id])["columns"][0]
    assert selected["logical_passages"][0]["speaker"] == "NARRATOR"
    assert selected["segments"][0]["speaker"] is None
    assert selected["segments"][0]["owned_passages"][0]["speaker"] == "NARRATOR"
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
    assert [item["speaker"] or "" for item in values] == ["", ""]
    saved = review_case.service.save_review(
        review_case.session.id,
        "correction",
        1,
        values,
        source_artifact_id=source_id,
        expected_source_hash=source_hash,
    )
    with review_case.database.session() as session:
        artifact = session.get(Artifact, saved["artifact_id"])
        rows = stored_passages(artifact)
        assert rows[0]["speaker"] == "NARRATOR"
        speech = materialize_speech_source(session, artifact)
        assert speech is not None
        speech_rows, speech_revision_id = speech
        assert speech_rows[0]["speaker"] == "NARRATOR"
        materialized = list(
            session.scalars(
                select(Segment)
                .where(Segment.revision_id == speech_revision_id)
                .order_by(Segment.ordinal)
            )
        )
        assert [item.speaker for item in materialized] == ["NARRATOR"]


def test_conflicting_display_fragment_speakers_reject_save_atomically(review_case):
    source_id, source_hash, _first_id, _second_id = _source(
        review_case, same_turn=True
    )
    with review_case.database.session() as session:
        source = session.get(Artifact, source_id)
        segments = list(
            session.scalars(
                select(Segment)
                .where(Segment.revision_id == source.metadata_json["revision_id"])
                .order_by(Segment.ordinal)
            )
        )
        attach_passages(
            source,
            [
                {
                    "id": "one-utterance",
                    "text": "First. Second.",
                    "start_ms": 0,
                    "end_ms": 2000,
                    "speaker": "",
                    "turn_id": "named-turn-a",
                    "source_cue_ids": [item.id for item in segments],
                }
            ],
            source=source,
        )
        source_rows = [item.id for item in segments]
        document_id = source.metadata_json["document_id"]
        source_revision_id = source.metadata_json["revision_id"]
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
    values[0]["speaker"] = "NARRATOR"
    values[1]["speaker"] = "CHARACTER"
    destination = review_case.session_dir / "reviewed_correction_r2.srt"
    with pytest.raises(ValueError, match="one-utterance.*align speakers or use a passage edit") as error:
        review_case.service.save_review(
            review_case.session.id,
            "correction",
            1,
            values,
            source_artifact_id=source_id,
            expected_source_hash=source_hash,
        )
    assert all(row_id in str(error.value) for row_id in source_rows)
    assert not destination.exists()
    with review_case.database.session() as session:
        document = session.get(Document, document_id)
        revisions = list(
            session.scalars(
                select(DocumentRevision).where(
                    DocumentRevision.document_id == document_id
                )
            )
        )
        assert document.active_revision_id == source_revision_id
        assert [revision.revision_number for revision in revisions] == [1]


def test_deleted_intro_before_display_reflow_has_unique_ids_and_direct_ancestry(
    review_case,
):
    artifact = review_case._artifact(
        "reflow-after-deletion.srt",
        "correction",
        "1\n00:00:00,000 --> 00:00:00,500\nIntro.\n\n"
        "2\n00:00:00,500 --> 00:00:01,500\nMiddle first.\n\n"
        "3\n00:00:01,500 --> 00:00:02,500\nMiddle second.\n\n"
        "4\n00:00:02,500 --> 00:00:03,000\nThird.\n\n"
        "5\n00:00:03,000 --> 00:00:03,500\nFourth.\n",
    )
    with review_case.database.session() as session:
        source = session.get(Artifact, artifact.id)
        segments = list(
            session.scalars(
                select(Segment)
                .where(Segment.revision_id == source.metadata_json["revision_id"])
                .order_by(Segment.ordinal)
            )
        )
        attach_passages(
            source,
            [
                {
                    "id": "p000001",
                    "text": "Intro.",
                    "start_ms": 0,
                    "end_ms": 500,
                    "speaker": "",
                    "turn_id": "turn-intro",
                    "source_cue_ids": [segments[0].id],
                },
                {
                    "id": "p000002",
                    "text": "Middle first. Middle second.",
                    "start_ms": 500,
                    "end_ms": 2500,
                    "speaker": "NARRATOR",
                    "turn_id": "turn-middle",
                    "source_cue_ids": [segments[1].id, segments[2].id],
                    "source_word_ids": ["middle-word-1", "middle-word-2"],
                    "review_state": "uncertain",
                    "evidence_ids": ["middle-evidence"],
                    "uncertain_source_cue_ids": [2],
                },
                {
                    "id": "p000003",
                    "text": "Third.",
                    "start_ms": 2500,
                    "end_ms": 3000,
                    "speaker": "THIRD",
                    "turn_id": "turn-third",
                    "source_cue_ids": [segments[3].id],
                },
                {
                    "id": "p000004",
                    "text": "Fourth.",
                    "start_ms": 3000,
                    "end_ms": 3500,
                    "speaker": "FOURTH",
                    "turn_id": "turn-fourth",
                    "source_cue_ids": [segments[4].id],
                },
            ],
            source=source,
        )
        source_hash = source.content_hash
    selected = review_case.service.review(
        review_case.session.id, [artifact.id]
    )["columns"][0]
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
        for item in selected["segments"][1:]
    ]
    saved = review_case.service.save_review(
        review_case.session.id,
        "correction",
        1,
        values,
        source_artifact_id=artifact.id,
        expected_source_hash=source_hash,
    )
    with review_case.database.session() as session:
        rows = stored_passages(session.get(Artifact, saved["artifact_id"]))
        ids = [row["id"] for row in rows]
        assert ids == ["p000001", "p000002", "p000003"]
        assert len(ids) == len(set(ids))
        assert [row["source_passage_ids"] for row in rows] == [
            ["p000002"],
            ["p000003"],
            ["p000004"],
        ]
        assert rows[0]["source_cue_ids"] == [segments[1].id, segments[2].id]
        assert rows[0]["source_word_ids"] == ["middle-word-1", "middle-word-2"]
        assert rows[0]["turn_id"] == "turn-middle"
        assert rows[0]["evidence_ids"] == ["middle-evidence"]
        assert rows[0]["uncertain_source_cue_ids"] == [2]


def _combined_passage_display_source(review_case):
    artifact = review_case._artifact(
        "combined-passage-display.srt",
        "correction",
        "1\n00:00:00,000 --> 00:00:02,000\nFirst. Second.\n",
    )
    with review_case.database.session() as session:
        source = session.get(Artifact, artifact.id)
        segment = session.scalar(
            select(Segment).where(
                Segment.revision_id == source.metadata_json["revision_id"]
            )
        )
        attach_passages(
            source,
            [
                {
                    "id": "p000001",
                    "text": "First.",
                    "start_ms": 0,
                    "end_ms": 1000,
                    "speaker": "ALICE",
                    "turn_id": "shared-turn",
                    "source_cue_ids": [segment.id],
                },
                {
                    "id": "p000002",
                    "text": "Second.",
                    "start_ms": 1000,
                    "end_ms": 2000,
                    "speaker": "BOB",
                    "turn_id": "shared-turn",
                    "source_cue_ids": [segment.id],
                },
            ],
            source=source,
        )
        return source.id, source.content_hash


def test_unchanged_combined_display_cue_preserves_each_source_speaker(review_case):
    source_id, source_hash = _combined_passage_display_source(review_case)
    selected = review_case.service.review(review_case.session.id, [source_id])["columns"][0]
    cue = selected["segments"][0]
    assert cue["edit_capabilities"]["ownership"] == "combined"
    assert all(
        cue["edit_capabilities"][action] is False
        for action in (
            "text",
            "display_timing",
            "speaker",
            "start_new_utterance",
            "split",
            "merge",
            "delete",
        )
    )
    assert "p000001" in cue["edit_capabilities"]["reason"]
    assert "p000002" in cue["edit_capabilities"]["reason"]
    assert "Edit spoken passages separately." in cue["edit_capabilities"]["reason"]
    assert [row["speaker"] for row in cue["owned_passages"]] == ["ALICE", "BOB"]
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
    saved = review_case.service.save_review(
        review_case.session.id,
        "correction",
        1,
        values,
        source_artifact_id=source_id,
        expected_source_hash=source_hash,
    )
    with review_case.database.session() as session:
        rows = stored_passages(session.get(Artifact, saved["artifact_id"]))
        assert [row["speaker"] for row in rows] == ["ALICE", "BOB"]


def test_combined_display_cue_rejects_speaker_reassignment(review_case):
    source_id, source_hash = _combined_passage_display_source(review_case)
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
    values[0]["speaker"] = "CHARLIE"
    with pytest.raises(ValueError, match="p000001, p000002.*align speakers or edit each passage separately"):
        review_case.service.save_review(
            review_case.session.id,
            "correction",
            1,
            values,
            source_artifact_id=source_id,
            expected_source_hash=source_hash,
        )


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
