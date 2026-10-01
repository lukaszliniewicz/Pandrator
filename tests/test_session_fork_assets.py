"""Forked media edits keep their exact rendered timeline and review evidence."""

from __future__ import annotations

import pytest
from sqlalchemy import select

from pandrator.web.jobs import JobQueue
from pandrator.web.logical_passages import attach_passages, stored_passages
from pandrator.web.media_edit import MediaEditService
from pandrator.web.models import (
    Artifact,
    ArtifactEdge,
    Document,
    DocumentRevision,
    MediaEditPlan,
    MediaEditPlanRevision,
    Segment,
    SessionRecord,
    SessionSetting,
    SubtitleEvidence,
)
from pandrator.web.session_forks import SessionForkService
from pandrator.web.subtitle_review import SubtitleReviewService
from pandrator.web.workflows import WorkflowService
from tests import test_web_session_forks as legacy_forks_tests


@pytest.fixture
def fork_case():
    case = legacy_forks_tests.WebSessionForkTests(methodName="runTest")
    case.setUp()
    try:
        yield case
    finally:
        case.tearDown()


def _edited_fixture(case, *, with_evidence: bool = True):
    """Make a rendered cut with a real dependency graph and opaque video bytes."""
    subtitles = case._checkpoint("media_edit_subtitles", "Cut timeline", case.transcription)
    media_path = case.session_dir / "edited.mp4"
    media_path.write_bytes(b"edited video bytes")
    media = case.artifacts.register(
        media_path,
        kind="video",
        role="media_edit_media",
        session_id=case.record["id"],
        parent_ids=[case.source.id, case.transcription.id],
    )
    timing_path = case.session_dir / "retimed.json"
    timing_path.write_text('{"words": []}', encoding="utf-8")
    timing = case.artifacts.register(
        timing_path,
        kind="json",
        role="media_edit_word_timestamps",
        session_id=case.record["id"],
        parent_ids=[subtitles.id],
    )
    with case.database.session() as session:
        plan = MediaEditPlan(session_id=case.record["id"])
        session.add(plan)
        session.flush()
        revision = MediaEditPlanRevision(
            plan_id=plan.id,
            revision_number=2,
            source_media_artifact_id=case.source.id,
            editorial_transcript_artifact_id=case.transcription.id,
            duration_ms=1000,
            instructions="Keep reviewed material",
            keep_ranges_json=[{"id": "keep-1", "start_ms": 0, "end_ms": 1000}],
            cues_json=[{"id": "cue-1", "start_ms": 0, "end_ms": 1000, "text": "Cut timeline"}],
            evidence_json={"reason": "fixture"},
            operation_json={"kind": "review"},
            reviewed=True,
            content_hash="pending",
        )
        revision.content_hash = MediaEditService._content_hash(
            MediaEditService._revision_snapshot(revision)
        )
        session.add(revision)
        session.flush()
        plan.active_revision_id = revision.id
        for original in (subtitles, media, timing):
            stored = session.get(Artifact, original.id)
            metadata = dict(stored.metadata_json or {})
            metadata.update(
                plan_id=plan.id,
                media_edit_revision_id=revision.id,
                content_hash=revision.content_hash,
            )
            if original.role == "media_edit_media":
                metadata["revision_id"] = revision.id
            stored.metadata_json = metadata
        session.add(
            ArtifactEdge(
                parent_artifact_id=subtitles.id,
                child_artifact_id=case.correction.id,
            )
        )
        if with_evidence:
            correction = session.get(Artifact, case.correction.id)
            source_segment = session.scalar(
                select(Segment).where(
                    Segment.revision_id
                    == session.get(Artifact, case.transcription.id).metadata_json["revision_id"]
                )
            )
            evidence = SubtitleEvidence(
                session_id=case.record["id"],
                source_artifact_id=case.transcription.id,
                source_media_artifact_id=case.source.id,
                source_revision_id=source_segment.revision_id,
                source_segment_id=source_segment.id,
                cue_id=1,
                start_ms=0,
                end_ms=1000,
                clip_start_ms=0,
                clip_end_ms=1000,
                reason="Uncertain word",
                routes_json=["whisper"],
                audio_model_ids_json=[],
                status="completed",
                candidates_json=[{"text": "Hello"}],
                resolution_json={"accepted": False},
            )
            session.add(evidence)
            session.flush()
            clip_path = case.session_dir / "evidence-clip.wav"
            clip_path.write_bytes(b"audio excerpt bytes")
            clip = case.artifacts.register_in_session(
                session,
                clip_path,
                kind="wav",
                role="subtitle_evidence_audio",
                session_id=case.record["id"],
                parent_ids=[case.source.id, case.transcription.id],
                metadata={
                    "evidence_id": evidence.id,
                    "source_artifact_id": case.transcription.id,
                    "source_media_artifact_id": case.source.id,
                },
            )
            transcript_path = case.session_dir / "evidence-transcript.json"
            transcript_path.write_text('{"text": "Hello"}', encoding="utf-8")
            transcript = case.artifacts.register_in_session(
                session,
                transcript_path,
                kind="json",
                role="subtitle_evidence_transcript",
                session_id=case.record["id"],
                parent_ids=[clip.id, case.transcription.id],
                metadata={"evidence_id": evidence.id},
            )
            evidence.clip_artifact_id = clip.id
            evidence.candidates_json = [
                {
                    "id": "candidate-1",
                    "status": "success",
                    "text": "Hello",
                    "transcript_artifact_id": transcript.id,
                }
            ]
            segment = session.scalar(
                select(Segment).where(
                    Segment.revision_id == correction.metadata_json["revision_id"]
                )
            )
            segment.metadata_json = {
                "turn_id": "turn-original",
                "review_state": "uncertain",
                "review_note": "Check the word",
                "evidence_ids": [evidence.id],
            }
            attach_passages(
                correction,
                [
                    {
                        "id": "passage-original",
                        "start_ms": 0,
                        "end_ms": 1000,
                        "text": "Hello!",
                        "speaker": "",
                        "turn_id": "turn-original",
                        "source_cue_ids": [segment.id],
                        "evidence_ids": [evidence.id],
                        "review_state": "uncertain",
                        "review_note": "Check the word",
                    }
                ],
                source=correction,
            )
            return plan.id, revision.id, (subtitles.id, media.id, timing.id), evidence.id
        return plan.id, revision.id, (subtitles.id, media.id, timing.id), None


def _fork(case, *, carry_media_assets=True, expected_revision=None, target_language=None):
    with case.database.session() as session:
        return SessionForkService(case.database, case.paths, case.artifacts).fork_in_session(
            session,
            case.record["id"],
            case.translation.id,
            carry_media_assets=carry_media_assets,
            expected_revision=expected_revision,
            target_language=target_language,
        )


def test_fork_carries_edit_render_and_review_evidence(fork_case):
    case = fork_case
    plan_id, revision_id, render_ids, evidence_id = _edited_fixture(case)
    result = _fork(case, target_language="de")
    assert len(result.copied_media_artifact_ids) == 3
    with case.database.session() as session:
        child_plan = session.scalar(
            select(MediaEditPlan).where(MediaEditPlan.session_id == result.record.id)
        )
        child_revision = session.get(MediaEditPlanRevision, child_plan.active_revision_id)
        assert child_plan.id != plan_id
        assert child_revision.id != revision_id
        assert child_revision.reviewed
        assert child_revision.content_hash == MediaEditService._content_hash(
            MediaEditService._revision_snapshot(child_revision)
        )
        assert child_revision.source_media_artifact_id == case.source.id
        assert (
            child_revision.editorial_transcript_artifact_id
            == result.artifact_id_map[case.transcription.id]
        )
        assert session.get(SessionRecord, result.record.id).target_language == "de"
        assert (
            session.get(SessionSetting, (result.record.id, "translation")).value_json[
                "target_language"
            ]
            == "de"
        )
        for old_id in render_ids:
            new_id = result.artifact_id_map[old_id]
            assert old_id != new_id
            original = session.get(Artifact, old_id)
            cloned = session.get(Artifact, new_id)
            assert cloned.session_id == result.record.id
            assert cloned.content_hash == original.content_hash
            assert (
                case.paths.managed_path(cloned.relative_path).read_bytes()
                == case.paths.managed_path(original.relative_path).read_bytes()
            )
            assert cloned.metadata_json["plan_id"] == child_plan.id
            assert cloned.metadata_json["media_edit_revision_id"] == child_revision.id
            assert cloned.metadata_json["content_hash"] == child_revision.content_hash
            if cloned.role != "media_edit_subtitles":
                assert cloned.metadata_json["revision_id"] == child_revision.id
        child_correction = session.get(Artifact, result.artifact_id_map[case.correction.id])
        assert session.get(
            ArtifactEdge, (result.artifact_id_map[render_ids[0]], child_correction.id)
        )
        child_segment = session.scalar(
            select(Segment).where(
                Segment.revision_id == child_correction.metadata_json["revision_id"]
            )
        )
        child_evidence_id = child_segment.metadata_json["evidence_ids"][0]
        child_evidence = session.get(SubtitleEvidence, child_evidence_id)
        assert child_evidence_id != evidence_id
        assert child_evidence.session_id == result.record.id
        assert child_evidence.job_id is None
        assert child_evidence.source_artifact_id == result.artifact_id_map[case.transcription.id]
        assert child_evidence.source_media_artifact_id == case.source.id
        assert child_evidence.clip_artifact_id != case.source.id
        assert session.get(Artifact, child_evidence.clip_artifact_id).session_id == result.record.id
        assert (
            session.get(Artifact, child_evidence.clip_artifact_id).metadata_json["evidence_id"]
            == child_evidence.id
        )
        candidate_transcript_id = child_evidence.candidates_json[0]["transcript_artifact_id"]
        assert session.get(Artifact, candidate_transcript_id).session_id == result.record.id
        assert (
            session.get(Artifact, candidate_transcript_id).metadata_json["evidence_id"]
            == child_evidence.id
        )
        assert child_evidence.resolution_json["forked_from_evidence_id"] == evidence_id
        assert child_segment.metadata_json["turn_id"] == "turn-original"
        assert child_segment.metadata_json["review_state"] == "uncertain"
        assert child_segment.metadata_json["review_note"] == "Check the word"
        passage = stored_passages(child_correction)[0]
        assert passage["source_cue_ids"] == [child_segment.id]
        assert passage["evidence_ids"] == [child_evidence_id]
        assert passage["turn_id"] == "turn-original"
        assert session.get(SubtitleEvidence, evidence_id).session_id == case.record["id"]

    resolved = WorkflowService(case.database, JobQueue(case.database)).resolve_stage(
        result.record.id,
        "export",
        {"export_mode": "media", "audio_mode": "preserve", "subtitle_mode": "none"},
    )
    assert (
        resolved.payload["export_contract"]["source_artifact_id"]
        == result.artifact_id_map[render_ids[1]]
    )

    review = SubtitleReviewService(
        case.database,
        case.artifacts,
        lambda _session_id: result.directory,
    )
    selected = review.review(result.record.id, [child_correction.id])["columns"][0]
    assert selected["segments"][0]["evidence_ids"] == [child_evidence_id]
    with pytest.raises(ValueError, match="Subtitle evidence must belong"):
        review.save_review(
            result.record.id,
            "correction",
            selected["revision"],
            [
                {
                    "id": child_segment.id,
                    "start_ms": 0,
                    "end_ms": 1000,
                    "text": "Hello revised",
                    "evidence_ids": [evidence_id],
                }
            ],
            source_artifact_id=child_correction.id,
            expected_source_hash=child_correction.content_hash,
        )
    review.save_review(
        result.record.id,
        "correction",
        selected["revision"],
        [
            {
                "id": child_segment.id,
                "start_ms": 0,
                "end_ms": 1000,
                "text": "Hello revised",
                "turn_id": "turn-original",
                "review_state": "uncertain",
                "review_note": "Check the word",
                "evidence_ids": [child_evidence_id],
            }
        ],
        source_artifact_id=child_correction.id,
        expected_source_hash=child_correction.content_hash,
    )
    with case.database.session() as session:
        assert (
            session.get(MediaEditPlanRevision, revision_id).content_hash
            != child_revision.content_hash
        )
        assert session.get(Artifact, render_ids[0]).state == "current"
        assert session.get(Artifact, case.correction.id).state == "current"
        assert session.get(SubtitleEvidence, evidence_id).session_id == case.record["id"]


def test_fork_rejects_missing_media_and_revision_conflict(fork_case):
    case = fork_case
    _, _, render_ids, _ = _edited_fixture(case, with_evidence=False)
    with pytest.raises(RuntimeError, match="revision conflict"):
        _fork(case, expected_revision=999)
    with case.database.session() as session:
        media_path = case.paths.managed_path(session.get(Artifact, render_ids[1]).relative_path)
    media_path.unlink()
    with pytest.raises(FileNotFoundError, match="missing"):
        _fork(case)
    with case.database.session() as session:
        assert (
            session.scalar(
                select(MediaEditPlan).where(MediaEditPlan.session_id != case.record["id"])
            )
            is None
        )


def test_explicit_opt_out_preserves_text_only_fork(fork_case):
    case = fork_case
    _edited_fixture(case, with_evidence=False)
    result = _fork(case, carry_media_assets=False)
    assert result.copied_media_artifact_ids == ()
    with case.database.session() as session:
        assert (
            session.scalar(
                select(MediaEditPlan).where(MediaEditPlan.session_id == result.record.id)
            )
            is None
        )
        assert {
            item.role
            for item in session.scalars(
                select(Artifact).where(Artifact.session_id == result.record.id)
            )
        } == {"transcription", "correction", "translation"}


def test_media_edit_invalidation_stays_inside_each_fork(fork_case):
    case = fork_case
    _, _, render_ids, _ = _edited_fixture(case, with_evidence=False)
    result = _fork(case)
    child_render_id = result.artifact_id_map[render_ids[0]]
    child_text_id = result.artifact_id_map[case.correction.id]
    child_translation_id = result.artifact_id_map[case.translation.id]
    with case.database.session() as session:
        assert session.get(ArtifactEdge, (render_ids[0], child_text_id)) is None
        MediaEditService._invalidate_rendered_outputs(session, case.record["id"])
    with case.database.session() as session:
        assert session.get(Artifact, render_ids[0]).state == "stale"
        assert session.get(Artifact, case.correction.id).state == "stale"
        assert session.get(Artifact, child_render_id).state == "current"
        assert session.get(Artifact, child_text_id).state == "current"
        assert session.get(Artifact, child_translation_id).state == "current"
        MediaEditService._invalidate_rendered_outputs(session, result.record.id)
    with case.database.session() as session:
        assert session.get(Artifact, child_render_id).state == "stale"
        assert session.get(Artifact, child_text_id).state == "stale"
        assert session.get(Artifact, child_translation_id).state == "stale"


def test_historical_evidence_source_stays_addressable_without_hiding_checkpoint(fork_case):
    case = fork_case
    historical_path = case.session_dir / "historical-transcription.srt"
    historical_path.write_text(
        "1\n00:00:00,000 --> 00:00:01,000\nOld wording\n",
        encoding="utf-8",
    )
    historical = case.artifacts.register(
        historical_path,
        kind="srt",
        role="transcription",
        session_id=case.record["id"],
        parent_ids=[case.source.id],
    )
    case.handlers._store_srt_document(
        case.record["id"],
        historical,
        "transcription",
        language="en",
        parent_artifact=None,
    )
    with case.database.session() as session:
        for artifact_id in (case.transcription.id, case.correction.id, case.translation.id):
            session.get(Artifact, artifact_id).state = "current"
        session.get(Artifact, historical.id).state = "stale"
        old_revision_id = session.get(Artifact, historical.id).metadata_json["revision_id"]
        old_segment = session.scalar(select(Segment).where(Segment.revision_id == old_revision_id))
        evidence = SubtitleEvidence(
            session_id=case.record["id"],
            source_artifact_id=historical.id,
            source_revision_id=old_revision_id,
            source_segment_id=old_segment.id,
            cue_id=1,
            start_ms=0,
            end_ms=1000,
            clip_start_ms=0,
            clip_end_ms=1000,
            reason="Historical uncertainty",
            status="completed",
            routes_json=["whisper"],
            audio_model_ids_json=[],
            candidates_json=[],
            resolution_json={},
        )
        session.add(evidence)
        session.flush()
        correction = session.get(Artifact, case.correction.id)
        segment = session.scalar(
            select(Segment).where(Segment.revision_id == correction.metadata_json["revision_id"])
        )
        segment.metadata_json = {"evidence_ids": [evidence.id], "review_state": "uncertain"}
        evidence_id = evidence.id
    result = _fork(case)
    with case.database.session() as session:
        child_historical = session.get(Artifact, result.artifact_id_map[historical.id])
        child_revision = session.get(
            DocumentRevision, child_historical.metadata_json["revision_id"]
        )
        child_document = session.get(Document, child_revision.document_id)
        assert child_document.active_revision_id is None
        assert child_document.stage == "transcription"
        child_correction = session.get(Artifact, result.artifact_id_map[case.correction.id])
        child_segment = session.scalar(
            select(Segment).where(
                Segment.revision_id == child_correction.metadata_json["revision_id"]
            )
        )
        cloned_evidence = session.get(
            SubtitleEvidence, child_segment.metadata_json["evidence_ids"][0]
        )
        assert cloned_evidence.id != evidence_id
        assert cloned_evidence.source_artifact_id == child_historical.id
        assert cloned_evidence.source_revision_id == child_revision.id
        assert cloned_evidence.source_segment_id != old_segment.id
        assert (
            session.get(Artifact, result.artifact_id_map[case.transcription.id]).state == "current"
        )
    review = SubtitleReviewService(case.database, case.artifacts, lambda _id: result.directory)
    selected = review.documents(result.record.id)["stages"]["transcription"]
    assert selected["segments"][0]["text"] == "Hello"


def test_pending_evidence_and_mismatched_render_are_rejected(fork_case):
    case = fork_case
    _, _, render_ids, evidence_id = _edited_fixture(case)
    with case.database.session() as session:
        session.get(SubtitleEvidence, evidence_id).status = "queued"
    with pytest.raises(ValueError, match="Pending subtitle evidence"):
        _fork(case)
    with case.database.session() as session:
        session.get(SubtitleEvidence, evidence_id).status = "completed"
        render = session.get(Artifact, render_ids[1])
        metadata = dict(render.metadata_json)
        metadata["content_hash"] = "wrong"
        render.metadata_json = metadata
    with pytest.raises(ValueError, match="does not match"):
        _fork(case)


def test_foreign_checkpoint_is_rejected(fork_case):
    case = fork_case
    first = _fork(case)
    with case.database.session() as session:
        with pytest.raises(KeyError):
            SessionForkService(case.database, case.paths, case.artifacts).fork_in_session(
                session,
                first.record.id,
                case.translation.id,
            )


def test_language_project_branches_keep_the_pinned_edited_media(fork_case):
    case = fork_case
    _edited_fixture(case)
    with case.database.session() as session:
        source_revision = session.get(SessionRecord, case.record["id"]).revision
    response = case.client.post(
        f"/api/v1/sessions/{case.record['id']}/translation-project",
        json={
            "checkpoint_artifact_id": case.correction.id,
            "expected_revision": source_revision,
        },
        headers={**case.headers, "Idempotency-Key": "edited-project-create"},
    )
    assert response.status_code == 200, response.get_json()
    project = response.get_json()["project"]
    response = case.client.post(
        f"/api/v1/translation-projects/{project['id']}/branches",
        json={
            "expected_revision": project["revision"],
            "targets": [{"target_language": "de"}, {"target_language": "ja"}],
        },
        headers={**case.headers, "Idempotency-Key": "edited-project-branches"},
    )
    assert response.status_code == 200, response.get_json()
    branches = response.get_json()["project"]["branches"]
    assert len(branches) == 2
    media_ids = set()
    with case.database.session() as session:
        for branch in branches:
            plan = session.scalar(
                select(MediaEditPlan).where(MediaEditPlan.session_id == branch["session_id"])
            )
            revision = session.get(MediaEditPlanRevision, plan.active_revision_id)
            media = session.scalar(
                select(Artifact).where(
                    Artifact.session_id == branch["session_id"],
                    Artifact.role == "media_edit_media",
                    Artifact.state == "current",
                )
            )
            assert media.metadata_json["media_edit_revision_id"] == revision.id
            assert media.metadata_json["content_hash"] == revision.content_hash
            assert (
                case.paths.managed_path(media.relative_path).read_bytes() == b"edited video bytes"
            )
            assert branch["source_content_hash"] == project["source_content_hash"]
            assert branch["translation_status"] == "ready"
            media_ids.add(media.id)
    assert len(media_ids) == 2
