"""Prepared subtitle reviews keep expensive work outside a guarded writer."""

from __future__ import annotations

import hashlib
import os
import random
import stat
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import fields
from threading import Barrier
from unittest.mock import patch
from uuid import uuid4

import pytest
from sqlalchemy import event, func, select
from sqlalchemy.orm import object_session

from pandrator.web.access.contracts import AccessDenied
from pandrator.web.accounts.auth_models import UserCredential
from pandrator.web.accounts.execution_actor import UserActor, current_execution_actor
from pandrator.web.accounts.identity import initial_personal_workspace
from pandrator.web.accounts.resource_grant_models import ApiTokenWorkspaceGrant
from pandrator.web.auth import Principal
from pandrator.web.credentials import SecretRedactor
from pandrator.web.idempotency import IdempotencyService
from pandrator.web.models import (
    ApiIdempotency,
    ApiToken,
    Artifact,
    Document,
    DocumentRevision,
    Segment,
    SegmentLineage,
    SubtitleEvidence,
    utcnow,
)
from pandrator.web.schemas import (
    SubtitlePassageInput,
    SubtitlePassageReviewRequest,
    SubtitleReviewRequest,
    SubtitleSegmentInput,
)
from pandrator.web.subtitle_review import (
    PreparedSubtitleReview,
    SubtitleReviewService,
    _review_passage_rows,
    cleanup_review_publications,
)
from pandrator.web.subtitle_review_mutations import SubtitleReviewMutationService
from pandrator.web.workspace_settings import WorkspaceSettingsService
from tests import test_web_subtitle_review as legacy
from tests.test_subtitle_passage_review import _source as _canonical_source
from tests.test_subtitle_review_turns import _source


@pytest.fixture
def review_case():
    case = legacy.SubtitleReviewTests(methodName="runTest")
    case.setUp()
    try:
        yield case
    finally:
        case.tearDown()


def _inputs(case):
    source_id, source_hash, *_ = _source(case, same_turn=True)
    column = case.service.review(case.session.id, [source_id])["columns"][0]
    return column, source_id, source_hash


def _prepare(case, kind="manual"):
    column, source_id, source_hash = _inputs(case)
    common = dict(source_artifact_id=source_id, expected_source_hash=source_hash)
    if kind == "canonical":
        prepared = case.service.prepare_passage_review(
            case.session.id, "correction", 1, column["logical_passages"],
            expected_composition_hash=column["composition_hash"], **common,
        )
    else:
        prepared = case.service.prepare_review(
            case.session.id, "correction", 1, column["segments"], **common,
        )
    assert isinstance(prepared, PreparedSubtitleReview)
    return prepared


def _counts(case):
    with case.database.snapshot_session() as session:
        return tuple(session.scalar(select(func.count()).select_from(model)) for model in (
            Artifact, DocumentRevision, Segment, SegmentLineage, ApiIdempotency,
        ))


def _publish(case, prepared):
    paths = []
    try:
        with case.database.immediate_session() as session:
            return case.service.publish_prepared_review_in_session(session, prepared, paths)
    except BaseException:
        cleanup_review_publications(paths)
        raise
    finally:
        prepared.cleanup()


@pytest.mark.parametrize("kind", ["manual", "canonical"])
def test_preparation_detaches_primitives_and_does_no_database_writes(review_case, kind):
    # Seed before listening: only preparation belongs to the read-only phase.
    column, source_id, source_hash = _inputs(review_case)
    before = _counts(review_case)
    statements = []

    def track(_connection, _cursor, statement, *_args):
        if statement.lstrip().split()[0].upper() in {"INSERT", "UPDATE", "DELETE", "REPLACE"}:
            statements.append(statement)

    event.listen(review_case.database.engine, "before_cursor_execute", track)
    try:
        if kind == "canonical":
            prepared = review_case.service.prepare_passage_review(
                review_case.session.id, "correction", 1, column["logical_passages"],
                source_artifact_id=source_id, expected_source_hash=source_hash,
                expected_composition_hash=column["composition_hash"],
            )
        else:
            prepared = review_case.service.prepare_review(
                review_case.session.id, "correction", 1, column["segments"],
                source_artifact_id=source_id, expected_source_hash=source_hash,
            )
    finally:
        event.remove(review_case.database.engine, "before_cursor_execute", track)
    try:
        assert not statements
        assert _counts(review_case) == before
        assert prepared.staged_path.exists()
        assert not prepared.destination.exists()
        assert prepared.registration.content_hash == hashlib.sha256(
            prepared.staged_path.read_bytes()
        ).hexdigest()

        def detached(value):
            if isinstance(value, dict):
                for item in value.values():
                    detached(item)
            elif isinstance(value, (list, tuple)):
                for item in value:
                    detached(item)
            elif isinstance(value, (Artifact, Document, DocumentRevision, Segment)):
                assert object_session(value) is None
                pytest.fail("Prepared review contains an ORM record")

        for field in fields(prepared):
            detached(getattr(prepared, field.name))
        directory_syncs = []

        def directory_sync(descriptor):
            assert stat.S_ISDIR(os.fstat(descriptor).st_mode), "file fsync in writer"
            assert prepared.destination.exists(), "directory synced before rename"
            directory_syncs.append(descriptor)

        # A writer uses precomputed bytes; only the renamed directory entry is
        # synced here so a durable DB commit cannot outrun file publication.
        with patch("pandrator.web.subtitle_review.compose_srt", side_effect=AssertionError("compose in writer")), patch(
            "pandrator.web.subtitle_review.subtitle_export_diagnostics", side_effect=AssertionError("diagnostics in writer")
        ), patch("pandrator.web.subtitle_review.sha256_file", side_effect=AssertionError("source hash in writer")), patch.object(
            review_case.artifacts, "prepare_registration", side_effect=AssertionError("file read in writer")
        ), patch("pandrator.web.subtitle_review.os.fsync", side_effect=directory_sync):
            result = _publish(review_case, prepared)
        if os.name != "nt":
            assert directory_syncs
        assert result["revision"] == 2
    finally:
        prepared.cleanup()


@pytest.mark.parametrize("kind", ["manual", "canonical"])
@pytest.mark.parametrize("change", ["source_metadata", "source_disk", "source_role", "source_state", "head", "max_revision", "settings"])
def test_stale_preparation_denies_before_publication(review_case, kind, change):
    prepared = _prepare(review_case, kind)
    if change == "source_disk":
        review_case.paths.managed_path(prepared.source["relative_path"]).write_text("Replaced source", encoding="utf-8")
    elif change == "settings":
        service = WorkspaceSettingsService(review_case.database)
        current = service.get(review_case.session.id, "subtitles")
        service.update(review_case.session.id, "subtitles", current["revision"], {"max_chars": 55})
    else:
        with review_case.database.immediate_session() as session:
            source = session.get(Artifact, prepared.source["id"])
            if change == "source_metadata":
                source.metadata_json = {**source.metadata_json, "changed": True}
            elif change == "source_role":
                source.role = "translation"
            elif change == "source_state":
                source.state = "deleted"
            elif change == "head":
                session.get(Document, prepared.document["id"]).active_revision_id = None
            else:
                session.add(DocumentRevision(
                    document_id=prepared.document["id"], revision_number=2,
                    content_hash="concurrent-passages", reviewed=False,
                ))
    before = _counts(review_case)
    with pytest.raises(RuntimeError):
        _publish(review_case, prepared)
    assert _counts(review_case) == before
    assert not prepared.destination.exists()
    assert not prepared.staged_path.exists()


def test_preparation_rechecks_evidence_status_and_window(review_case):
    column, source_id, source_hash = _inputs(review_case)
    evidence_id = str(uuid4())
    with review_case.database.session() as session:
        session.add(SubtitleEvidence(
            id=evidence_id, session_id=review_case.session.id,
            source_artifact_id=source_id, source_revision_id=column["revision_id"],
            source_segment_id=column["segments"][0]["id"], cue_id=1,
            start_ms=0, end_ms=1000, clip_start_ms=0, clip_end_ms=2000,
            reason="Check", status="completed",
        ))
    values = [{**column["segments"][0], "evidence_ids": [evidence_id]}, column["segments"][1]]
    for field, replacement in (("status", "running"), ("start_ms", 1100)):
        prepared = review_case.service.prepare_review(
            review_case.session.id, "correction", 1, values,
            source_artifact_id=source_id, expected_source_hash=source_hash,
        )
        with review_case.database.session() as session:
            setattr(session.get(SubtitleEvidence, evidence_id), field, replacement)
        before = _counts(review_case)
        with pytest.raises(RuntimeError, match="evidence changed"):
            _publish(review_case, prepared)
        assert _counts(review_case) == before
        assert not prepared.destination.exists()
        with review_case.database.session() as session:
            setattr(session.get(SubtitleEvidence, evidence_id), field, "completed" if field == "status" else 0)


def _actor(case):
    with case.database.session() as session:
        workspace = initial_personal_workspace(session)
        user_id = workspace.personal_owner_user_id
        credential = session.scalar(select(UserCredential).where(UserCredential.user_id == user_id))
        token_id = str(uuid4())
        scopes = frozenset({"app.read", "app.write"})
        session.add(ApiToken(
            id=token_id, label="Review preparation synthetic token", token_hash=hashlib.sha256(token_id.encode()).hexdigest(),
            token_prefix=token_id[:12], subject="prepared-review-test", scopes_json=sorted(scopes),
            principal_kind="api_token", user_id=user_id, user_auth_revision=credential.auth_revision,
        ))
        session.flush()
        session.add(ApiTokenWorkspaceGrant(token_id=token_id, workspace_id=workspace.id))
        return UserActor(user_id=user_id, auth_revision=credential.auth_revision,
                         subject="prepared-review-test", principal_kind="api_token", scopes=scopes, token_id=token_id)


@pytest.mark.parametrize("kind", ["manual", "canonical"])
def test_revocation_between_prepare_and_publish_denies_and_cleans(review_case, kind):
    actor = _actor(review_case)
    token = current_execution_actor.set(actor)
    try:
        prepared = _prepare(review_case, kind)
        with review_case.database.session() as session:
            session.get(ApiToken, actor.token_id).revoked_at = utcnow()
        before = _counts(review_case)
        with pytest.raises(AccessDenied):
            _publish(review_case, prepared)
        assert _counts(review_case) == before
        assert not prepared.destination.exists()
        assert not prepared.staged_path.exists()
        # Source reads must also be denied after revocation.
        with patch.object(review_case.service, "_source_file_witness", side_effect=AssertionError("read before access")):
            with pytest.raises(AccessDenied):
                review_case.service.prepare_review(
                    review_case.session.id, "correction", 1, [{"start_ms": 0, "end_ms": 10, "text": "Denied"}],
                    source_artifact_id=prepared.source["id"],
                )
    finally:
        current_execution_actor.reset(token)


def test_same_prepared_head_has_one_concurrent_winner(review_case):
    column, source_id, source_hash = _inputs(review_case)
    prepared = [review_case.service.prepare_review(
        review_case.session.id, "correction", 1, column["segments"],
        source_artifact_id=source_id, expected_source_hash=source_hash,
    ) for _ in range(2)]
    barrier = Barrier(2)

    def run(value):
        barrier.wait(timeout=5)
        try:
            return _publish(review_case, value)
        except RuntimeError as error:
            return error

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(run, prepared))
    assert sum(isinstance(result, dict) for result in results) == 1
    assert sum(isinstance(result, RuntimeError) for result in results) == 1
    assert sum(value.destination.exists() for value in prepared) == 1
    assert not any(value.staged_path.exists() for value in prepared)
    with review_case.database.snapshot_session() as session:
        assert list(session.scalars(select(DocumentRevision.revision_number).where(
            DocumentRevision.document_id == prepared[0].document["id"]
        ).order_by(DocumentRevision.revision_number))) == [1, 2]


@pytest.mark.parametrize("failure", ["stage_fsync", "move", "register", "commit"])
def test_io_and_commit_failure_remove_only_attempt_files(review_case, failure):
    column, source_id, source_hash = _inputs(review_case)
    sentinel = review_case.session_dir / "reviewed_correction_r2.srt"
    sentinel.write_text("Pre-existing unrelated revision file", encoding="utf-8")
    before = _counts(review_case)
    before_files = set(review_case.session_dir.iterdir())
    if failure == "stage_fsync":
        target = patch("pandrator.web.subtitle_review.os.fsync", side_effect=OSError("stage fsync failed"))
    elif failure == "move":
        target = patch("pandrator.web.subtitle_review.os.replace", side_effect=OSError("move failed"))
    elif failure == "register":
        target = patch.object(review_case.artifacts, "register_in_session", side_effect=RuntimeError("register failed"))
    else:
        original = review_case.database.immediate_session

        @contextmanager
        def failed_commit(*args, **kwargs):
            with original(*args, **kwargs) as session:
                yield session
                raise RuntimeError("commit failed")

        target = patch.object(review_case.database, "immediate_session", failed_commit)
    with target, pytest.raises((RuntimeError, OSError)):
        review_case.service.save_review(
            review_case.session.id, "correction", 1, column["segments"],
            source_artifact_id=source_id, expected_source_hash=source_hash,
        )
    assert _counts(review_case) == before
    assert set(review_case.session_dir.iterdir()) == before_files
    assert sentinel.read_text(encoding="utf-8") == "Pre-existing unrelated revision file"


def test_explicit_destination_collision_is_preserved(review_case):
    prepared = _prepare(review_case)
    prepared.destination.write_text("Another owner's bytes", encoding="utf-8")
    before = _counts(review_case)
    with pytest.raises(RuntimeError, match="path already exists"):
        _publish(review_case, prepared)
    assert _counts(review_case) == before
    assert prepared.destination.read_text(encoding="utf-8") == "Another owner's bytes"
    assert not prepared.staged_path.exists()


def _request_rows(rows, model):
    return [{key: value for key, value in row.items() if key in model.model_fields} for row in rows]


def _mutation(case):
    return SubtitleReviewMutationService(
        case.database, IdempotencyService(case.database, SecretRedactor(case.database)), case.service,
    )


def _principal():
    return Principal(subject="review-test", kind="owner_session", scopes=frozenset({"app.read", "app.write"}),
                     token_id=None, network_zone="loopback", target_instance_id="review-test")


@pytest.mark.parametrize("kind", ["manual", "canonical"])
def test_idempotent_replay_skips_preparation_and_returns_exact_response(review_case, kind):
    column, source_id, source_hash = _inputs(review_case)
    service = _mutation(review_case)
    data = dict(expected_revision=1, source_artifact_id=source_id, expected_source_hash=source_hash)
    if kind == "canonical":
        payload = SubtitlePassageReviewRequest(
            **data, expected_composition_hash=column["composition_hash"], passages=_request_rows(column["logical_passages"], SubtitlePassageInput),
        )
        save = service.save_passage_review
        prepare_name = "prepare_passage_review"
    else:
        payload = SubtitleReviewRequest(**data, segments=_request_rows(column["segments"], SubtitleSegmentInput))
        save = service.save_review
        prepare_name = "prepare_review"
    first = save(review_case.session.id, "correction", payload, principal=_principal(), idempotency_key="prepared-review-replay")
    before = _counts(review_case)
    files_before = set(review_case.session_dir.iterdir())
    with patch.object(review_case.service, prepare_name, side_effect=AssertionError("replay prepared again")):
        replay = save(review_case.session.id, "correction", payload, principal=_principal(), idempotency_key="prepared-review-replay")
    assert replay.replayed
    assert replay.status_code == first.status_code == 201
    assert replay.payload == first.payload
    assert _counts(review_case) == before
    assert set(review_case.session_dir.iterdir()) == files_before


def test_concurrent_same_key_replays_losing_preparation(review_case):
    column, source_id, source_hash = _inputs(review_case)
    service = _mutation(review_case)
    payload = SubtitleReviewRequest(
        expected_revision=1, source_artifact_id=source_id, expected_source_hash=source_hash,
        segments=_request_rows(column["segments"], SubtitleSegmentInput),
    )
    original = review_case.service.prepare_review
    barrier = Barrier(2)

    def prepare(*args, **kwargs):
        value = original(*args, **kwargs)
        # Both requests stage before either tries the reservation writer.
        barrier.wait(timeout=10)
        return value

    def save(_index):
        return service.save_review(review_case.session.id, "correction", payload,
                                   principal=_principal(), idempotency_key="concurrent-review-replay")

    with patch.object(review_case.service, "prepare_review", prepare):
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(save, range(2)))
    assert results[0].payload == results[1].payload
    assert sorted(item.replayed for item in results) == [False, True]
    assert len(list(review_case.session_dir.glob("reviewed_correction_r2_*.srt"))) == 1
    assert not list(review_case.session_dir.glob(".*.tmp"))


@pytest.mark.parametrize("kind", ["manual", "canonical"])
def test_dry_run_stages_no_file_and_reserves_no_key(review_case, kind):
    column, source_id, source_hash = _inputs(review_case)
    data = dict(dry_run=True, expected_revision=1, source_artifact_id=source_id, expected_source_hash=source_hash)
    service = _mutation(review_case)
    before = _counts(review_case)
    files_before = set(review_case.session_dir.iterdir())
    if kind == "canonical":
        payload = SubtitlePassageReviewRequest(
            **data, expected_composition_hash=column["composition_hash"], passages=_request_rows(column["logical_passages"], SubtitlePassageInput),
        )
        save = service.save_passage_review
    else:
        payload = SubtitleReviewRequest(**data, segments=_request_rows(column["segments"], SubtitleSegmentInput))
        save = service.save_review
    with patch.object(review_case.database, "immediate_session", side_effect=AssertionError("dry run writer")), patch(
        "pandrator.web.subtitle_review.tempfile.mkstemp", side_effect=AssertionError("dry run stage")
    ):
        result = save(review_case.session.id, "correction", payload, principal=_principal(), idempotency_key="dry-run-no-reservation")
    assert result.status_code == 200
    assert result.payload["validated"]
    assert _counts(review_case) == before
    assert set(review_case.session_dir.iterdir()) == files_before


def test_comparison_nested_unsorted_duplicate_and_touching_intervals_match_reference(review_case):
    randomizer = random.Random(1781)
    bounds = [(0, 100), (0, 10), (10, 20), (20, 90), (5, 95), (100, 110), (0, 100)]
    bounds.extend((start, start + randomizer.randrange(1, 80)) for start in (randomizer.randrange(100) for _ in range(40)))
    randomizer.shuffle(bounds)
    stages = {name: [Segment(id=f"{name}-{index}", ordinal=index,
                             start_ms=start, end_ms=end, text=f"{name} {index}")
                     for index, (start, end) in enumerate(values)]
              for name, values in (("correction", bounds[::2]), ("translation", bounds[1::2]))}
    parents = {item.id: item.id for rows in stages.values() for item in rows}

    def root(key):
        while parents[key] != key:
            key = parents[key]
        return key

    for left in stages["correction"]:
        for right in stages["translation"]:
            if min(left.end_ms, right.end_ms) > max(left.start_ms, right.start_ms):
                parents[root(right.id)] = root(left.id)
    expected = {}
    for key in parents:
        expected.setdefault(root(key), set()).add(key)
    with review_case.database.snapshot_session() as session:
        rows = review_case.service._comparison_rows_by_key(session, stages, list(stages))
    actual = [frozenset(item["id"] for stage in stages for item in row[stage]) for row in rows]
    assert set(actual) == {frozenset(group) for group in expected.values()}
    for row in rows:
        for stage in stages:
            assert [item["ordinal"] for item in row[stage]] == sorted(item["ordinal"] for item in row[stage])


class _CountedRows(list):
    visited = 0

    def __iter__(self):
        for item in super().__iter__():
            self.visited += 1
            yield item


def test_many_cues_batch_passage_ownership_and_candidates_with_linear_visits():
    size = 1000
    segments = [Segment(id=f"cue-{index}", ordinal=index, start_ms=index * 1000,
                        end_ms=index * 1000 + 900, text=f"Text {index}", metadata_json={})
                for index in range(size)]
    passages = _CountedRows([
        {"id": f"p{index:06d}", "start_ms": item.start_ms, "end_ms": item.end_ms,
         "text": item.text, "speaker": "", "source_cue_ids": [item.id, item.id]}
        for index, item in enumerate(segments)
    ])
    payload = SubtitleReviewService._payloads_with_passages(segments, passages)
    assert passages.visited <= 4 * size
    assert all(row["source_passage_ids"] == [passages[index]["id"]] for index, row in enumerate(payload))
    passages.visited = 0
    normalized = [{**row, "starts_new_turn": False, "origin_segment_id": None,
                   "split_boundary_id": None} for row in payload]
    output = _review_passage_rows(passages, segments, normalized, source_hash="source")
    assert len(output) == size
    assert passages.visited <= 8 * size


def test_payload_batch_preserves_exact_canonical_order_and_temporal_fallback():
    passages = [
        {"id": "outer", "start_ms": 0, "end_ms": 100, "text": "Outer", "source_cue_ids": ["owned", "owned"]},
        {"id": "inner", "start_ms": 20, "end_ms": 40, "text": "Inner", "source_cue_ids": ["owned"]},
        {"id": "touch", "start_ms": 100, "end_ms": 200, "text": "Touch"},
    ]
    segments = [
        Segment(id="owned", ordinal=0, start_ms=0, end_ms=100, text="Owned", metadata_json={}),
        Segment(id="canonical", ordinal=1, start_ms=20, end_ms=40, text="Canonical", metadata_json={
            "canonical_passage_review": True, "source_passage_ids": ["inner", "outer", "inner"],
        }),
        Segment(id="fallback", ordinal=2, start_ms=30, end_ms=50, text="Fallback", metadata_json={}),
    ]
    old_shape = [SubtitleReviewService._payload_with_passages(item, passages) for item in segments]
    batched = SubtitleReviewService._payloads_with_passages(segments, passages)
    assert batched == old_shape
    assert [row["source_passage_ids"] for row in batched] == [
        ["outer", "inner"], ["inner", "outer", "inner"], ["outer", "inner"],
    ]


def test_nested_interval_union_does_not_drop_shorter_same_side_rows(review_case):
    stages = {
        "correction": [
            Segment(id="outer", ordinal=0, start_ms=0, end_ms=100, text="Outer"),
            Segment(id="inner", ordinal=1, start_ms=1, end_ms=2, text="Inner"),
        ],
        "translation": [
            Segment(id="right", ordinal=0, start_ms=0, end_ms=3, text="Right"),
            Segment(id="touch", ordinal=1, start_ms=100, end_ms=110, text="Touch"),
        ],
    }
    with review_case.database.snapshot_session() as session:
        rows = review_case.service._comparison_rows_by_key(session, stages, list(stages))
    assert len(rows) == 2
    assert [item["id"] for item in rows[0]["correction"]] == ["outer", "inner"]
    assert [item["id"] for item in rows[0]["translation"]] == ["right"]
    assert [item["id"] for item in rows[1]["translation"]] == ["touch"]


def test_prepared_lineage_preserves_source_ordinal_for_every_nested_overlap(review_case):
    bounds = [(2000, 3000), (0, 10000), (1000, 1500), (3000, 4000)]
    source_id, source_hash, _ = _canonical_source(
        review_case,
        "1\n00:00:02,000 --> 00:00:03,000\nFirst.\n\n"
        "2\n00:00:00,000 --> 00:00:10,000\nOuter.\n\n"
        "3\n00:00:01,000 --> 00:00:01,500\nInner.\n\n"
        "4\n00:00:03,000 --> 00:00:04,000\nTouch.\n",
        [{"id": f"p{index}", "text": f"Passage {index}", "start_ms": start, "end_ms": end,
          "speaker": "A", "cue_indices": [index]} for index, (start, end) in enumerate(bounds)],
    )
    column = review_case.service.review(review_case.session.id, [source_id])["columns"][0]
    prepared = review_case.service.prepare_passage_review(
        review_case.session.id, "correction", 1, column["logical_passages"],
        source_artifact_id=source_id, expected_source_hash=source_hash,
        expected_composition_hash=column["composition_hash"],
    )
    try:
        for row in prepared.children:
            expected = [item["id"] for item in column["segments"]
                        if min(row["end_ms"], item["end_ms"]) > max(row["start_ms"], item["start_ms"])]
            assert row["parent_ids"] == expected
            assert len(row["metadata_json"]["source_passage_ids"]) == 1
        result = _publish(review_case, prepared)
        with review_case.database.snapshot_session() as session:
            children = list(session.scalars(select(Segment).where(
                Segment.revision_id == result["revision_id"]
            ).order_by(Segment.ordinal)))
            for child, row in zip(children, prepared.children, strict=True):
                actual = list(session.scalars(select(SegmentLineage.parent_segment_id).where(
                    SegmentLineage.child_segment_id == child.id
                ).order_by(SegmentLineage.sequence)))
                assert actual == row["parent_ids"]
    finally:
        prepared.cleanup()


def test_old_selected_revision_can_publish_again_when_captured_head_is_unchanged(review_case):
    column, source_id, source_hash = _inputs(review_case)
    common = dict(source_artifact_id=source_id, expected_source_hash=source_hash)
    first = review_case.service.save_review(
        review_case.session.id, "correction", 1, column["segments"], **common,
    )
    prepared = review_case.service.prepare_review(
        review_case.session.id, "correction", 1, column["segments"], **common,
    )
    assert prepared.previous["id"] == column["revision_id"]
    assert prepared.document["active_revision_id"] == first["revision_id"]
    result = _publish(review_case, prepared)
    assert result["revision"] == 3
    with review_case.database.snapshot_session() as session:
        assert session.get(DocumentRevision, result["revision_id"]).parent_revision_id == column["revision_id"]


def test_source_change_between_streaming_hash_and_post_stat_is_rejected(review_case):
    column, source_id, source_hash = _inputs(review_case)
    from pandrator.web.artifacts import sha256_file

    def changed_during_hash(path):
        digest = sha256_file(path)
        path.write_text("Changed after read, before the second stat", encoding="utf-8")
        return digest

    before = _counts(review_case)
    with patch("pandrator.web.subtitle_review.sha256_file", changed_during_hash):
        with pytest.raises(RuntimeError, match="source content hash changed"):
            review_case.service.prepare_review(
                review_case.session.id, "correction", 1, column["segments"],
                source_artifact_id=source_id, expected_source_hash=source_hash,
            )
    assert _counts(review_case) == before
    assert not list(review_case.session_dir.glob("reviewed_*.srt"))
    assert not list(review_case.session_dir.glob(".*.tmp"))


def test_payload_batch_does_not_join_dense_intervals_with_explicit_ownership():
    passages = [{"id": f"p{index}", "start_ms": 0, "end_ms": 1000,
                 "text": "Nested", "source_cue_ids": [f"cue-{index}"]}
                for index in range(100)]
    segments = [Segment(id=f"cue-{index}", ordinal=index, start_ms=0, end_ms=1000,
                        text="Nested", metadata_json={}) for index in range(100)]
    with patch("pandrator.web.subtitle_review.overlap_indexes", side_effect=AssertionError("owned cues need no fallback join")):
        result = SubtitleReviewService._payloads_with_passages(segments, passages)
    assert [item["source_passage_ids"] for item in result] == [[f"p{index}"] for index in range(100)]


def test_publication_directory_sync_failure_is_portable_best_effort(review_case):
    prepared = _prepare(review_case, "display")
    synced = []
    closed = []
    real_close = os.close

    def unsupported(descriptor):
        assert stat.S_ISDIR(os.fstat(descriptor).st_mode)
        assert prepared.destination.exists()
        synced.append(descriptor)
        raise OSError("directory sync unsupported")

    def close(descriptor):
        closed.append(descriptor)
        return real_close(descriptor)

    with patch("pandrator.web.subtitle_review.os.fsync", side_effect=unsupported), patch(
        "pandrator.web.subtitle_review.os.close", side_effect=close
    ):
        result = _publish(review_case, prepared)
    assert result["revision"] == 2
    if os.name != "nt":
        assert synced and set(synced).issubset(closed)
    assert prepared.destination.exists()
