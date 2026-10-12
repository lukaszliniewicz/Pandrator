"""MCP editing preserves canonical identity across display reflow."""

from __future__ import annotations

from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from pydantic import ValidationError

from pandrator_mcp.clients.application import ApplicationClient
from pandrator_mcp.errors import PandratorMcpError
from pandrator_mcp.schemas.sessions import ReplaceSubtitleTextInput
from pandrator_mcp.schemas.subtitle_passages import (
    PatchSubtitlePassagesInput,
    PreviewSubtitlePassagesInput,
)
from pandrator_mcp.tools.sessions import replace_subtitle_text
from pandrator_mcp.tools.subtitle_passages import (
    patch_subtitle_passages,
    preview_subtitle_passages,
)
from tests import test_web_subtitle_review as legacy_review_tests
from tests.test_subtitle_passage_review import _source


@pytest.fixture
def case():
    fixture = legacy_review_tests.SubtitleReviewTests(methodName="runTest")
    fixture.setUp()
    try:
        yield fixture
    finally:
        fixture.tearDown()


class ReviewApplication:
    def __init__(self, case):
        self.case = case
        self.calls = []

    def get_subtitles(self, session_id):
        return self.case.service.documents(session_id)

    def get_subtitle_catalog(self, session_id):
        return self.case.service.catalog(session_id)

    def review_subtitles(self, session_id, *, artifact_ids):
        return self.case.service.review(session_id, artifact_ids)

    def save_subtitle_review(self, session_id, stage, *, segments, idempotency_key, **kwargs):
        self.calls.append(deepcopy(segments))
        return self.case.service.save_review(session_id, stage, values=segments, **kwargs)

    def save_subtitle_passage_review(self, session_id, stage, *, passages, idempotency_key, **kwargs):
        self.calls.append(deepcopy(passages))
        return self.case.service.save_passage_review(session_id, stage, values=passages, **kwargs)


def test_legacy_replace_preserves_reflow_identity_and_dry_run_validates(case):
    source_id, _, _ = _source(case,
        "1\n00:00:00,000 --> 00:00:01,000\nFilomena begins\n\n"
        "2\n00:00:01,000 --> 00:00:02,000\nthe discussion.\n",
        [{"id": "p-one", "text": "Filomena begins the discussion.", "start_ms": 0,
          "end_ms": 2000, "speaker": "", "turn_id": "t-one", "cue_indices": [0, 1]}])
    application = ReviewApplication(case)
    runtime = SimpleNamespace(require_application=lambda: application)
    arguments = ReplaceSubtitleTextInput(session_id=case.session.id, stage="correct",
        expected_revision=1, search_text="Filomena", replacement_text="Filomena Nunes",
        dry_run=True, idempotency_key="name-review")
    preview = replace_subtitle_text(runtime, arguments).result
    assert preview["validated"] is True
    assert case.service.review(case.session.id, [source_id])["columns"][0]["revision"] == 1
    assert all(row["id"] and row["source_passage_ids"] == ["p-one"] for row in application.calls[0])
    saved = replace_subtitle_text(runtime, arguments.model_copy(update={"dry_run": False})).result
    column = case.service.review(case.session.id, [saved["artifact_id"]])["columns"][0]
    assert column["logical_passages"][0]["text"] == "Filomena Nunes begins the discussion."


def test_sparse_passage_text_and_turn_edit_preserves_other_passages(case):
    source_id, _, _ = _source(case,
        "1\n00:00:00,000 --> 00:00:04,000\nFirst passage. Yes.\n",
        [{"id": "p-one", "text": "First passage.", "start_ms": 0, "end_ms": 3000,
          "speaker": "", "turn_id": "same"},
         {"id": "p-two", "text": "Yes.", "start_ms": 3000, "end_ms": 4000,
          "speaker": "", "turn_id": "same"}])
    application = ReviewApplication(case)
    runtime = SimpleNamespace(require_application=lambda: application)
    preview = preview_subtitle_passages(runtime, PreviewSubtitlePassagesInput(
        session_id=case.session.id, artifact_id=source_id, query="Yes", limit=1))
    assert preview["total_passages"] == 2
    assert [row["id"] for row in preview["passages"]] == ["p-two"]
    assert "source_word_ids" not in preview["passages"][0]
    args = PatchSubtitlePassagesInput(session_id=case.session.id, source_artifact_id=source_id,
        expected_source_hash=preview["source_content_hash"], expected_revision=preview["revision"],
        expected_composition_hash=preview["composition_hash"],
        passages=[{"id": "p-two", "text": "Yes, I did.", "starts_new_turn": True}],
        idempotency_key="sparse-review", dry_run=True)
    assert patch_subtitle_passages(runtime, args).result["validated"] is True
    saved = patch_subtitle_passages(runtime, args.model_copy(update={"dry_run": False})).result
    rows = case.service.review(case.session.id, [saved["artifact_id"]])["columns"][0]["logical_passages"]
    assert rows[0]["text"] == "First passage."
    assert rows[1]["text"] == "Yes, I did."
    assert rows[0]["turn_id"] != rows[1]["turn_id"]
    assert all(not row.get("speaker") for row in rows)
    assert len(application.calls[-1]) == 2
    assert "source_word_ids" not in application.calls[-1][0]
    for changed in ({"expected_source_hash": "stale"}, {"expected_composition_hash": "stale"}):
        with pytest.raises(PandratorMcpError, match="changed"):
            patch_subtitle_passages(runtime, args.model_copy(update=changed))


def test_passage_client_preserves_guards_and_dry_run_transport():
    application = object.__new__(ApplicationClient)
    with patch.object(application, "_request_json", return_value={}) as request:
        application.save_subtitle_passage_review("s", "correction", source_artifact_id="a",
            expected_source_hash="h", expected_revision=3, expected_composition_hash="c",
            passages=[{"id": "p", "text": "Text"}], idempotency_key="review-1", dry_run=True)
    assert request.call_args.args == ("/api/v1/sessions/s/subtitles/correction/passage-review",)
    assert request.call_args.kwargs["body"]["dry_run"] is True
    assert request.call_args.kwargs["body"]["expected_composition_hash"] == "c"
    assert request.call_args.kwargs["idempotency_key"] == "review-1"


def test_duplicate_or_forged_passage_fields_are_rejected():
    base = dict(session_id="s", source_artifact_id="a", expected_source_hash="h",
        expected_revision=1, expected_composition_hash="c", idempotency_key="review-1")
    for passages in ([{"id": "p", "text": "A"}, {"id": "p", "text": "B"}],
                     [{"id": "p", "text": "A", "source_word_ids": ["invented"]}],
                     [{"id": "p", "starts_new_turn": "true"}]):
        with pytest.raises(ValidationError):
            PatchSubtitlePassagesInput(**base, passages=passages)
