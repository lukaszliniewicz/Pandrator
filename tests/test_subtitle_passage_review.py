"""Canonical passage review persists source-owned edits and bounded display cues."""

from __future__ import annotations

import tempfile
import unittest
from copy import deepcopy
from unittest.mock import patch
from uuid import uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy import event, select

from pandrator.web.api import create_app
from pandrator.web.auth import BootstrapTokenStore
from pandrator.web.logical_passages import (
    attach_passages,
    materialize_speech_source,
    stored_passages,
)
from pandrator.web.models import (
    Artifact,
    Document,
    DocumentRevision,
    Segment,
    SubtitleEvidence,
    TimedWord,
)
from pandrator.web.schemas import SubtitlePassageReviewRequest
from pandrator.web.workspace_settings import WorkspaceSettingsService
from tests import test_web_subtitle_review as legacy_review_tests
from tests.web_test_support import prepare_web_test_data_root


@pytest.fixture
def review_case():
    case = legacy_review_tests.SubtitleReviewTests(methodName="runTest")
    case.setUp()
    try:
        yield case
    finally:
        case.tearDown()


def _source(case, srt: str, rows: list[dict], *, language: str = "en"):
    artifact = case._artifact(
        f"canonical-passage-{uuid4().hex}.srt", "correction", srt
    )
    with case.database.session() as session:
        managed = session.get(Artifact, artifact.id)
        document = session.get(Document, managed.metadata_json["document_id"])
        document.language = language
        segments = list(
            session.scalars(
                select(Segment)
                .where(Segment.revision_id == managed.metadata_json["revision_id"])
                .order_by(Segment.ordinal)
            )
        )
        prepared = []
        for raw in rows:
            row = deepcopy(raw)
            cue_indices = row.pop("cue_indices", [0])
            row["source_cue_ids"] = [segments[index].id for index in cue_indices]
            prepared.append(row)
        attach_passages(managed, prepared, source=managed)
        return managed.id, managed.content_hash, managed.metadata_json["revision_id"]


def _edits(rows: list[dict], **overrides) -> list[dict]:
    output = []
    for row in rows:
        edit = {
            "id": row["id"],
            "text": row["text"],
            "speaker": row.get("speaker", ""),
            "start_ms": row["start_ms"],
            "end_ms": row["end_ms"],
            "starts_new_turn": False,
            "review_state": row.get("review_state", "clear"),
            "review_note": row.get("review_note", ""),
            "deleted": False,
        }
        edit.update(overrides.get(row["id"], {}))
        output.append(edit)
    return output


def _save(case, source_id, source_hash, revision, values, *, composition_hash=None):
    column = case.service.review(case.session.id, [source_id])["columns"][0]
    return case.service.save_passage_review(
        case.session.id,
        "correction",
        revision,
        values,
        source_artifact_id=source_id,
        expected_source_hash=source_hash,
        expected_composition_hash=composition_hash or column["composition_hash"],
    )


def _saved_rows(case, result):
    with case.database.session() as session:
        artifact = session.get(Artifact, result["artifact_id"])
        rows = stored_passages(artifact)
        segments = list(
            session.scalars(
                select(Segment)
                .where(Segment.revision_id == result["revision_id"])
                .order_by(Segment.ordinal)
            )
        )
        return deepcopy(rows), segments, artifact


def test_combined_display_can_edit_each_canonical_passage_independently(review_case):
    source_id, source_hash, _revision_id = _source(
        review_case,
        "1\n00:00:00,000 --> 00:00:04,000\nFirst. Second.\n",
        [
            {
                "id": "spoken-a",
                "text": "First.",
                "start_ms": 0,
                "end_ms": 2000,
                "speaker": "ALICE",
                "turn_id": "turn-a",
                "source_passage_ids": ["ancestor-a"],
            },
            {
                "id": "spoken-b",
                "text": "Second.",
                "start_ms": 2000,
                "end_ms": 4000,
                "speaker": "BOB",
                "turn_id": "turn-b",
                "source_passage_ids": ["ancestor-b"],
            },
        ],
    )
    column = review_case.service.review(review_case.session.id, [source_id])["columns"][0]
    assert column["segments"][0]["edit_capabilities"]["ownership"] == "combined"
    source_rows = column["logical_passages"]
    values = _edits(
        source_rows,
        **{
            "spoken-a": {"text": "First edited."},
            "spoken-b": {"text": "Second edited."},
        },
    )

    result = _save(review_case, source_id, source_hash, 1, values)
    rows, segments, artifact = _saved_rows(review_case, result)

    assert [(row["id"], row["text"]) for row in rows] == [
        ("spoken-a", "First edited."),
        ("spoken-b", "Second edited."),
    ]
    assert [row["source_passage_ids"] for row in rows] == [
        ["ancestor-a"],
        ["ancestor-b"],
    ]
    assert [item.metadata_json["source_passage_ids"] for item in segments] == [
        ["spoken-a"],
        ["spoken-b"],
    ]
    assert all(item.metadata_json["canonical_passage_review"] for item in segments)
    reopened = review_case.service.review(review_case.session.id, [artifact.id])["columns"][0]
    assert [item["edit_capabilities"]["ownership"] for item in reopened["segments"]] == [
        "exact",
        "exact",
    ]


def test_fragmented_passage_edits_preserve_anchors_evidence_and_materialized_speaker(
    review_case,
):
    source_id, source_hash, revision_id = _source(
        review_case,
        "1\n00:00:00,000 --> 00:00:02,000\nFirst fragment.\n\n"
        "2\n00:00:02,000 --> 00:00:04,000\nSecond fragment.\n",
        [
            {
                "id": "whole-utterance",
                "text": "First fragment. Second fragment.",
                "start_ms": 0,
                "end_ms": 4000,
                "speaker": "OLD VOICE",
                "turn_id": "turn-source",
                "source_passage_ids": ["upstream-passage"],
                "review_state": "uncertain",
                "review_note": "Keep this evidence.",
                "evidence_ids": ["source-evidence"],
                "uncertain_source_cue_ids": [1],
                "cue_indices": [0, 1],
            }
        ],
    )
    with review_case.database.session() as session:
        managed = session.get(Artifact, source_id)
        segments = list(
            session.scalars(
                select(Segment)
                .where(Segment.revision_id == revision_id)
                .order_by(Segment.ordinal)
            )
        )
        word_ids = []
        for ordinal, (word, start, end) in enumerate(
            [("First", 0, 700), ("fragment", 700, 1900), ("Second", 2100, 2800), ("fragment", 2800, 3900)]
        ):
            word = TimedWord(
                revision_id=revision_id,
                segment_id=segments[0 if ordinal < 2 else 1].id,
                ordinal=ordinal,
                text=word,
                start_ms=start,
                end_ms=end,
            )
            session.add(word)
            session.flush()
            word_ids.append(word.id)
        row = stored_passages(managed)[0]
        row["source_word_ids"] = word_ids
        source_cue_ids = list(row["source_cue_ids"])
        attach_passages(managed, [row], source=managed)
        session.add(
            SubtitleEvidence(
                id="source-evidence",
                session_id=review_case.session.id,
                source_artifact_id=source_id,
                source_revision_id=revision_id,
                source_segment_id=segments[0].id,
                cue_id=1,
                start_ms=0,
                end_ms=4000,
                clip_start_ms=0,
                clip_end_ms=4000,
                reason="Keep this evidence.",
                routes_json=["whisper"],
                audio_model_ids_json=[],
                status="completed",
                candidates_json=[],
                resolution_json={},
            )
        )
    column = review_case.service.review(review_case.session.id, [source_id])["columns"][0]
    assert [row["edit_capabilities"]["ownership"] for row in column["segments"]] == [
        "fragment",
        "fragment",
    ]
    edited = _edits(
        column["logical_passages"],
        **{
            "whole-utterance": {
                "text": "Reassembled canonical sentence.",
                "speaker": "NARRATOR",
                "review_note": "Retain the source evidence.",
            }
        },
    )
    result = _save(review_case, source_id, source_hash, 1, edited)
    rows, segments, artifact = _saved_rows(review_case, result)

    assert rows[0]["text"] == "Reassembled canonical sentence."
    assert rows[0]["speaker"] == "NARRATOR"
    assert rows[0]["source_word_ids"] == word_ids
    assert rows[0]["source_cue_ids"] == source_cue_ids
    assert rows[0]["source_passage_ids"] == ["upstream-passage"]
    assert rows[0]["evidence_ids"] == ["source-evidence"]
    assert rows[0]["uncertain_source_cue_ids"] == [1]
    assert rows[0]["turn_id"] == "turn-source"
    assert all(item.speaker == "NARRATOR" for item in segments)
    with review_case.database.session() as session:
        materialized = materialize_speech_source(
            session, session.get(Artifact, artifact.id)
        )
        assert materialized is not None
        materialized_rows, materialized_revision_id = materialized
        passage_segment = session.scalar(
            select(Segment).where(Segment.revision_id == materialized_revision_id)
        )
        assert passage_segment.speaker == "NARRATOR"
        assert passage_segment.metadata_json["logical_passage"]["source_word_ids"] == word_ids
        assert passage_segment.metadata_json["logical_passage"]["evidence_ids"] == [
            "source-evidence"
        ]
        assert materialized_rows[0]["turn_id"] == "turn-source"


def test_cjk_reflow_stays_inside_the_canonical_passage_window(review_case):
    long_japanese = "これは字幕の再構成と表示幅を確認するための日本語テキストです。" * 4
    source_id, source_hash, _revision_id = _source(
        review_case,
        "1\n00:00:00,000 --> 00:00:20,000\n元の字幕です。\n",
        [
            {
                "id": "japanese-passage",
                "text": "元の字幕です。",
                "start_ms": 0,
                "end_ms": 20000,
                "speaker": "NARRATOR",
            }
        ],
        language="ja",
    )
    values = _edits(
        review_case.service.review(review_case.session.id, [source_id])["columns"][0][
            "logical_passages"
        ],
        **{"japanese-passage": {"text": long_japanese}},
    )

    result = _save(review_case, source_id, source_hash, 1, values)
    rows, segments, _artifact = _saved_rows(review_case, result)

    assert rows[0]["text"] == long_japanese
    assert len(segments) > 1
    assert all(0 <= item.start_ms < item.end_ms <= 20000 for item in segments)
    assert all(item.speaker == "NARRATOR" for item in segments)
    assert all(
        item.metadata_json["source_passage_ids"] == ["japanese-passage"]
        for item in segments
    )


def test_new_utterance_turn_carries_until_existing_source_turn_boundary(review_case):
    source_id, source_hash, _revision_id = _source(
        review_case,
        "1\n00:00:00,000 --> 00:00:01,000\nFirst.\n\n"
        "2\n00:00:01,000 --> 00:00:02,000\nSecond.\n\n"
        "3\n00:00:02,000 --> 00:00:03,000\nThird.\n",
        [
            {"id": "turn-row-a", "text": "First.", "start_ms": 0, "end_ms": 1000, "speaker": "A", "turn_id": "turn-source-a"},
            {"id": "turn-row-b", "text": "Second.", "start_ms": 1000, "end_ms": 2000, "speaker": "A", "turn_id": "turn-source-a"},
            {"id": "turn-row-c", "text": "Third.", "start_ms": 2000, "end_ms": 3000, "speaker": "B", "turn_id": "turn-source-b"},
        ],
    )
    column = review_case.service.review(review_case.session.id, [source_id])["columns"][0]
    values = _edits(
        column["logical_passages"],
        **{"turn-row-a": {"starts_new_turn": True}},
    )
    result = _save(review_case, source_id, source_hash, 1, values)
    rows, _segments, _artifact = _saved_rows(review_case, result)

    assert rows[0]["turn_id"].startswith("turn-")
    assert rows[0]["turn_id"] == rows[1]["turn_id"]
    assert rows[0]["turn_id"] != "turn-source-a"
    assert rows[2]["turn_id"] == "turn-source-b"


def test_new_cross_speaker_overlap_is_rejected_but_inherited_crosstalk_is_kept(
    review_case,
):
    source_id, source_hash, _revision_id = _source(
        review_case,
        "1\n00:00:00,000 --> 00:00:02,000\nFirst.\n\n"
        "2\n00:00:02,000 --> 00:00:04,000\nSecond.\n",
        [
            {"id": "cross-a", "text": "First.", "start_ms": 0, "end_ms": 2000, "speaker": "A"},
            {"id": "cross-b", "text": "Second.", "start_ms": 2000, "end_ms": 4000, "speaker": "B"},
        ],
    )
    source_rows = review_case.service.review(review_case.session.id, [source_id])["columns"][0][
        "logical_passages"
    ]
    crossing = _edits(
        source_rows,
        **{"cross-a": {"end_ms": 2500}},
    )
    with pytest.raises(ValueError, match="cross-speaker overlap"):
        _save(review_case, source_id, source_hash, 1, crossing)

    overlap_id, overlap_hash, _revision_id = _source(
        review_case,
        "1\n00:00:00,000 --> 00:00:03,000\nFirst.\n\n"
        "2\n00:00:02,000 --> 00:00:04,000\nSecond.\n",
        [
            {"id": "inherited-a", "text": "First.", "start_ms": 0, "end_ms": 3000, "speaker": "A"},
            {"id": "inherited-b", "text": "Second.", "start_ms": 2000, "end_ms": 4000, "speaker": "B"},
        ],
    )
    overlap_rows = review_case.service.review(review_case.session.id, [overlap_id])["columns"][0][
        "logical_passages"
    ]
    result = _save(review_case, overlap_id, overlap_hash, 1, _edits(overlap_rows))
    saved, _segments, _artifact = _saved_rows(review_case, result)
    assert [(row["start_ms"], row["end_ms"]) for row in saved] == [
        (0, 3000),
        (2000, 4000),
    ]


@pytest.mark.parametrize(
    ("kind", "message"),
    [
        ("missing", "every selected source passage"),
        ("duplicate", "Duplicate source passage id"),
        ("foreign", "every selected source passage"),
        ("all_deleted", "cannot be empty"),
    ],
)
def test_passage_set_must_be_exact_and_retain_speech(review_case, kind, message):
    source_id, source_hash, _revision_id = _source(
        review_case,
        "1\n00:00:00,000 --> 00:00:01,000\nFirst.\n\n"
        "2\n00:00:01,000 --> 00:00:02,000\nSecond.\n",
        [
            {"id": "set-a", "text": "First.", "start_ms": 0, "end_ms": 1000, "speaker": "A"},
            {"id": "set-b", "text": "Second.", "start_ms": 1000, "end_ms": 2000, "speaker": "B"},
        ],
    )
    rows = review_case.service.review(review_case.session.id, [source_id])["columns"][0][
        "logical_passages"
    ]
    values = _edits(rows)
    if kind == "missing":
        values = values[:1]
    elif kind == "duplicate":
        values = [values[0], values[0], values[1]]
    elif kind == "foreign":
        values[1]["id"] = "foreign-row"
    else:
        for value in values:
            value["deleted"] = True
    with pytest.raises(ValueError, match=message):
        _save(review_case, source_id, source_hash, 1, values)


def test_explicit_passage_deletion_keeps_other_canonical_identity(review_case):
    source_id, source_hash, _revision_id = _source(
        review_case,
        "1\n00:00:00,000 --> 00:00:01,000\nFirst.\n\n"
        "2\n00:00:01,000 --> 00:00:02,000\nSecond.\n",
        [
            {"id": "delete-a", "text": "First.", "start_ms": 0, "end_ms": 1000, "speaker": "A"},
            {"id": "delete-b", "text": "Second.", "start_ms": 1000, "end_ms": 2000, "speaker": "B"},
        ],
    )
    rows = review_case.service.review(review_case.session.id, [source_id])["columns"][0][
        "logical_passages"
    ]
    values = _edits(rows, **{"delete-a": {"deleted": True}})
    result = _save(review_case, source_id, source_hash, 1, values)
    saved, segments, _artifact = _saved_rows(review_case, result)
    assert [row["id"] for row in saved] == ["delete-b"]
    assert all(item.metadata_json["source_passage_ids"] == ["delete-b"] for item in segments)


def test_source_revision_hash_and_composition_hash_are_optimistic_guards(review_case):
    source_id, source_hash, _revision_id = _source(
        review_case,
        "1\n00:00:00,000 --> 00:00:02,000\nOriginal.\n",
        [{"id": "guarded", "text": "Original.", "start_ms": 0, "end_ms": 2000, "speaker": "A"}],
    )
    values = _edits(
        review_case.service.review(review_case.session.id, [source_id])["columns"][0][
            "logical_passages"
        ]
    )
    with pytest.raises(RuntimeError, match="revision"):
        _save(review_case, source_id, source_hash, 2, values)
    with pytest.raises(RuntimeError, match="hash"):
        _save(review_case, source_id, "wrong-source-hash", 1, values)

    column = review_case.service.review(review_case.session.id, [source_id])["columns"][0]
    with review_case.database.immediate_session() as session:
        WorkspaceSettingsService(review_case.database).update_in_session(
            session,
            review_case.session.id,
            "subtitles",
            0,
            {"max_chars_per_line": 41},
        )
    with pytest.raises(RuntimeError, match="composition settings changed"):
        _save(
            review_case,
            source_id,
            source_hash,
            1,
            values,
            composition_hash=column["composition_hash"],
        )


def test_registration_failure_rolls_back_passage_revision_and_published_file(review_case):
    source_id, source_hash, _revision_id = _source(
        review_case,
        "1\n00:00:00,000 --> 00:00:02,000\nOriginal.\n",
        [{"id": "rollback", "text": "Original.", "start_ms": 0, "end_ms": 2000, "speaker": "A"}],
    )
    values = _edits(
        review_case.service.review(review_case.session.id, [source_id])["columns"][0][
            "logical_passages"
        ],
        **{"rollback": {"text": "Changed."}},
    )
    destination = review_case.session_dir / "reviewed_correction_r2.srt"
    with patch.object(
        review_case.artifacts,
        "register_in_session",
        side_effect=RuntimeError("register failed"),
    ):
        with pytest.raises(RuntimeError, match="register failed"):
            _save(review_case, source_id, source_hash, 1, values)
    assert not destination.exists()
    with review_case.database.session() as session:
        artifact = session.get(Artifact, source_id)
        revisions = list(
            session.scalars(
                select(DocumentRevision).where(
                    DocumentRevision.document_id
                    == artifact.metadata_json["document_id"]
                )
            )
        )
        assert [row.revision_number for row in revisions] == [1]


def test_passage_request_rejects_client_owned_ancestry_and_word_ids():
    request = {
        "source_artifact_id": "source",
        "expected_source_hash": "hash",
        "expected_revision": 1,
        "expected_composition_hash": "settings-hash",
        "passages": [
            {
                "id": "p000001",
                "text": "Text.",
                "speaker": "A",
                "start_ms": 0,
                "end_ms": 1000,
                "source_word_ids": ["forged-word"],
            }
        ],
    }
    with pytest.raises(ValidationError):
        SubtitlePassageReviewRequest.model_validate(request)


def test_four_selected_columns_share_a_bounded_composition_snapshot(review_case):
    artifacts = [
        review_case._artifact(
            f"bounded-passage-{index}.srt",
            "correction",
            f"1\n00:00:00,000 --> 00:00:02,000\nColumn {index}.\n",
        )
        for index in range(4)
    ]
    select_count = 0

    def count_selects(_connection, _cursor, statement, _parameters, _context, _executemany):
        nonlocal select_count
        if statement.lstrip().upper().startswith("SELECT"):
            select_count += 1

    event.listen(review_case.database.engine, "before_cursor_execute", count_selects)
    try:
        payload = review_case.service.review(
            review_case.session.id, [artifact.id for artifact in artifacts]
        )
    finally:
        event.remove(review_case.database.engine, "before_cursor_execute", count_selects)

    assert len(payload["columns"]) == 4
    assert len({column["composition_hash"] for column in payload["columns"]}) == 1
    assert len({str(column["composition_settings"]) for column in payload["columns"]}) == 1
    # The settings snapshot is resolved once for the whole request, so adding
    # the remaining selected columns stays under this measured four-column cap.
    assert select_count <= 39


class PassageReviewRouteTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        prepare_web_test_data_root(self.temporary.name)
        bootstrap = BootstrapTokenStore()
        token = bootstrap.issue()
        self.app = create_app(
            data_root=self.temporary.name,
            testing=True,
            bootstrap_tokens=bootstrap,
            background_maintenance=False,
        )
        self.client = self.app.test_client()
        csrf = self.client.post(
            "/api/v1/auth/bootstrap", json={"token": token}
        ).get_json()["csrf_token"]
        self.headers = {"X-CSRF-Token": csrf}
        self.services = self.app.extensions["pandrator"]["services"]
        self.session = self.services.sessions.create(
            "Canonical passage route", workflow_kind="subtitles"
        )
        self.session_dir = (
            self.services.paths.sessions / self.session.storage_key
        )
        self.session_dir.mkdir(parents=True, exist_ok=True)
        path = self.session_dir / "route-source.srt"
        path.write_text(
            "1\n00:00:00,000 --> 00:00:02,000\nOriginal.\n",
            encoding="utf-8",
        )
        artifact = self.services.artifacts.register(
            path,
            kind="srt",
            role="correction",
            session_id=self.session.id,
        )
        self.services.workflow_handlers._store_srt_document(
            self.session.id, artifact, "correction"
        )
        with self.services.database.session() as session:
            managed = session.get(Artifact, artifact.id)
            segment = session.scalar(
                select(Segment).where(
                    Segment.revision_id == managed.metadata_json["revision_id"]
                )
            )
            attach_passages(
                managed,
                [
                    {
                        "id": "route-passage",
                        "text": "Original.",
                        "start_ms": 0,
                        "end_ms": 2000,
                        "speaker": "NARRATOR",
                        "source_cue_ids": [segment.id],
                    }
                ],
                source=managed,
            )
            self.source_id = managed.id
            self.source_hash = managed.content_hash
            self.source_revision = session.get(
                DocumentRevision, managed.metadata_json["revision_id"]
            ).revision_number
        self.column = self.services.subtitle_review.review(
            self.session.id, [self.source_id]
        )["columns"][0]

    def tearDown(self):
        self.app.extensions["pandrator"]["database"].dispose()
        self.temporary.cleanup()

    def test_canonical_route_replays_idempotent_response(self):
        response_body = {
            "source_artifact_id": self.source_id,
            "expected_source_hash": self.source_hash,
            "expected_revision": self.source_revision,
            "expected_composition_hash": self.column["composition_hash"],
            "passages": [
                {
                    "id": "route-passage",
                    "text": "Reviewed through the canonical route.",
                    "speaker": "NARRATOR",
                    "start_ms": 0,
                    "end_ms": 2000,
                    "starts_new_turn": False,
                    "review_state": "clear",
                    "review_note": "",
                    "deleted": False,
                }
            ],
        }
        headers = {**self.headers, "Idempotency-Key": "canonical-passage-review-1"}
        path = (
            f"/api/v1/sessions/{self.session.id}/subtitles/"
            "correction/passage-review"
        )
        first = self.client.post(path, json=response_body, headers=headers)
        replay = self.client.post(path, json=response_body, headers=headers)

        self.assertEqual(201, first.status_code, first.get_json())
        self.assertEqual(201, replay.status_code, replay.get_json())
        self.assertEqual("true", replay.headers.get("Idempotency-Replayed"))
        self.assertEqual(first.get_json(), replay.get_json())
        with self.services.database.session() as session:
            document = session.scalar(
                select(Document).where(
                    Document.session_id == self.session.id,
                    Document.stage == "correction",
                )
            )
            revisions = list(
                session.scalars(
                    select(DocumentRevision).where(
                        DocumentRevision.document_id == document.id
                    )
                )
            )
            self.assertEqual([1, 2], [row.revision_number for row in revisions])
