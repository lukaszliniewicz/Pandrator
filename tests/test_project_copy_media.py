"""Saved media-edit plans and their current registry bindings copy as a graph."""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace

import pytest
from sqlalchemy import event, select

from pandrator.web import models as m
from pandrator.web.export_inputs import resolve_export_inputs, select_media_export
from pandrator.web.media_edit import MediaEditService
from pandrator.web.model_base import new_id
from pandrator.web.projects import copy_snapshot as capture_module
from pandrator.web.projects.copy_contracts import COPY_STATE_VERSION, ProjectCopyError
from pandrator.web.projects.copy_media import REVISION_FIELDS, remap_revision, revision_hash
from pandrator.web.projects.copy_publication import copy_identity
from pandrator.web.workflow_output_context import OutputWorkflowContext
from tests.project_copy_test_support import (
    capture_with_connections,
    snapshot_destination_connections,
)
from tests.test_project_copy_publication import _context, _publish, _stage
from tests.test_project_copy_publication import publication_case as _publication_case
from tests.test_project_copy_snapshot import copy_case as _copy_case
from tests.test_project_export_bundles import bundle_case as _bundle_case

bundle_case = _bundle_case
copy_case = _copy_case
publication_case = _publication_case


def _media_service(case):
    return MediaEditService(
        case.database,
        case.services.artifacts,
        lambda _session_id: case.services.paths.sessions,
        duration_probe=lambda _path: 5_000,
    )


def _register_edit_inputs(case):
    media_path = case.services.paths.root / f"{new_id()}.mp4"
    media_bytes = b"copy media-edit fixture bytes"
    media_path.write_bytes(media_bytes)
    media = case.services.artifacts.register(
        media_path,
        kind="video",
        role="upload",
        session_id=case.source.id,
        workspace_id=case.source_workspace,
        metadata={"original_filename": "source.mp4"},
    )

    captions_path = case.services.paths.root / f"{new_id()}.vtt"
    captions_bytes = (
        "WEBVTT\n\n"
        "one\n00:00:01.000 --> 00:00:02.500\nNarrator: Keep this sentence.\n\n"
        "two\n00:00:02.600 --> 00:00:04.000\nNarrator: Preserve the next one.\n"
    ).encode()
    captions_path.write_bytes(captions_bytes)
    captions = case.services.artifacts.register(
        captions_path,
        kind="vtt",
        role="captions",
        session_id=case.source.id,
        workspace_id=case.source_workspace,
        metadata={"original_filename": "captions.vtt"},
    )
    with case.database.immediate_session() as session:
        session.get(m.SessionRecord, case.source.id).workflow_kind = "media_edit"
        for artifact, role, kind, contents in (
            (media, "primary", "video", media_bytes),
            (captions, "transcript", "vtt", captions_bytes),
        ):
            asset = m.SourceAsset(
                workspace_id=case.source_workspace,
                artifact_id=artifact.id,
                display_name=artifact.relative_path,
                kind=kind,
                size_bytes=len(contents),
                content_hash=artifact.content_hash,
            )
            session.add(asset)
            session.flush()
            session.add(
                m.SessionSource(
                    session_id=case.source.id,
                    source_asset_id=asset.id,
                    role=role,
                    is_current=True,
                )
            )
    return media, captions, media_bytes, captions_bytes


def _capture(case, **kwargs):
    with case.database.snapshot_session() as session:
        return capture_with_connections(
            case,
            session,
            _context(case.principal),
            case.source.project_id,
            case.destination,
            name="Media edit copy",
            settings_service=case.services.workspace_settings,
            **kwargs,
        )


def _output_context(case):
    def session_record(session_id):
        with case.database.session() as session:
            record = session.get(m.SessionRecord, session_id)
            assert record is not None
            session.expunge(record)
            return record

    return OutputWorkflowContext(
        case.database,
        case.services.paths,
        case.services.artifacts,
        case.services.artifacts.resolve,
        lambda session_id: case.services.paths.sessions / session_id,
        session_record,
        lambda session_id, operation: (
            case.services.paths.sessions / session_id / "operations" / operation
        ),
    )


def _snapshot_with_state(snapshot, state):
    encoded = json.dumps(
        state,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return replace(
        snapshot,
        state_json=encoded,
        fingerprint=hashlib.sha256(encoded.encode("utf-8")).hexdigest(),
    )


def test_active_edit_copies_independently_and_omits_unpinned_history(publication_case):
    case = publication_case
    media, captions, media_bytes, captions_bytes = _register_edit_inputs(case)
    before_plan = _capture(case)
    before_rows = json.loads(before_plan.state_json)["rows"]
    assert before_rows["media_edit_plans"] == []
    assert before_rows["media_edit_plan_revisions"] == []

    service = _media_service(case)
    prepared = service.prepare(case.source.id)
    assert prepared["plan"]["revision"] == 1
    unpinned_revision_id = prepared["plan"]["revision_id"]
    edited = service.update(
        case.source.id,
        1,
        keep_ranges=[{"start_ms": 0, "end_ms": 4_500, "label": "Reviewed cut"}],
        instructions="Keep the reviewed excerpt.",
        reviewed=True,
    )
    assert edited["plan"]["revision"] == 2

    with case.database.immediate_session() as session:
        session.add(m.MediaEditPlan(session_id=case.branches[0]["session_id"]))

    with case.database.snapshot_session() as session:
        source_plan = session.scalar(
            select(m.MediaEditPlan).where(m.MediaEditPlan.session_id == case.source.id)
        )
        source_revision = session.get(m.MediaEditPlanRevision, source_plan.active_revision_id)
        source_payload = {key: getattr(source_revision, key) for key in REVISION_FIELDS}
        assert revision_hash(source_payload) == MediaEditService._content_hash(
            MediaEditService._revision_snapshot(source_revision)
        ) == source_revision.content_hash
        original_rows = {
            "plan": (source_plan.id, source_plan.session_id, source_plan.active_revision_id),
            "revision": (
                source_revision.id,
                source_revision.plan_id,
                source_revision.parent_revision_id,
                source_revision.revision_number,
                source_revision.content_hash,
                json.loads(json.dumps(source_revision.keep_ranges_json)),
                json.loads(json.dumps(source_revision.cues_json)),
                json.loads(json.dumps(source_revision.evidence_json)),
                json.loads(json.dumps(source_revision.operation_json)),
                source_revision.instructions,
                source_revision.reviewed,
            ),
        }
    source_bytes = {
        media.id: case.services.paths.managed_path(media.relative_path).read_bytes(),
        captions.id: case.services.paths.managed_path(captions.relative_path).read_bytes(),
    }
    assert source_bytes == {media.id: media_bytes, captions.id: captions_bytes}

    ready_snapshot = _capture(case)
    assert ready_snapshot.ready, ready_snapshot.blockers
    destination_connections = snapshot_destination_connections(ready_snapshot)
    media_queries = []

    def capture_media_sql(_connection, _cursor, statement, parameters, _context, _many):
        if statement.lstrip().upper().startswith("SELECT") and "media_edit_plan_revisions" in statement:
            media_queries.append((statement, parameters))

    event.listen(case.database.engine, "before_cursor_execute", capture_media_sql)
    try:
        snapshot = _capture(case, destination_connections=destination_connections)
    finally:
        event.remove(case.database.engine, "before_cursor_execute", capture_media_sql)
    state = json.loads(snapshot.state_json)
    rows = state["rows"]
    assert snapshot.ready, snapshot.blockers
    assert state["schema_version"] == COPY_STATE_VERSION
    assert len(rows["media_edit_plans"]) == 2
    assert sum(row["active_revision_id"] is not None for row in rows["media_edit_plans"]) == 1
    assert len(rows["media_edit_plan_revisions"]) == 1
    copied_source_revision = rows["media_edit_plan_revisions"][0]
    assert copied_source_revision["id"] == source_revision.id
    assert copied_source_revision["revision_number"] == 2
    assert copied_source_revision["id"] == original_rows["revision"][0]
    assert copied_source_revision["id"] != unpinned_revision_id
    assert all(
        row["id"] != unpinned_revision_id for row in rows["media_edit_plan_revisions"]
    )
    assert copied_source_revision["parent_revision_id"] is None
    assert copied_source_revision["keep_ranges_json"] == [
        {"id": "keep-000001", "start_ms": 0, "end_ms": 4_500, "label": "Reviewed cut"}
    ]
    assert copied_source_revision["instructions"] == "Keep the reviewed excerpt."
    assert copied_source_revision["reviewed"] is True
    assert copied_source_revision["cues_json"] == edited["plan"]["cues"]
    assert copied_source_revision["content_hash"] == original_rows["revision"][4]
    assert rows["media_edit_plans"] and any(
        row["active_revision_id"] is None for row in rows["media_edit_plans"]
    )
    body_queries = [
        (statement, parameters)
        for statement, parameters in media_queries
        if "keep_ranges_json" in statement
    ]
    assert len(body_queries) == 1
    body_statement, body_parameters = body_queries[0]
    assert "JOIN media_edit_plans" in body_statement
    assert source_revision.id in str(body_parameters)
    assert unpinned_revision_id not in str(body_parameters)

    case.copy_id = new_id()
    case.snapshot = snapshot
    case.staged = _stage(case, snapshot)
    with case.database.immediate_session() as session:
        result = _publish(session, case, snapshot, case.staged)

    copied_plan_id = copy_identity(case.copy_id, "media_edit_plans", original_rows["plan"][0])
    copied_revision_id = copy_identity(
        case.copy_id, "media_edit_plan_revisions", original_rows["revision"][0]
    )
    copied_state = _media_service(case).state(result.source_session_id)
    assert copied_state["plan"]["revision_id"] == copied_revision_id
    assert copied_state["plan"]["revision"] == 2
    assert copied_state["plan"]["parent_revision_id"] is None
    assert copied_state["plan"]["keep_ranges"] == copied_source_revision["keep_ranges_json"]
    assert copied_state["plan"]["cues"] == copied_source_revision["cues_json"]
    assert copied_state["plan"]["instructions"] == copied_source_revision["instructions"]
    assert copied_state["plan"]["reviewed"] is True
    assert copied_state["plan"]["content_hash"] != copied_source_revision["content_hash"]
    with case.database.snapshot_session() as session:
        assert session.get(m.MediaEditPlan, copied_plan_id).active_revision_id == copied_revision_id
    with case.database.snapshot_session() as session:
        copied_revision = session.get(m.MediaEditPlanRevision, copied_revision_id)
        assert copied_revision is not None
        assert revision_hash(
            {key: getattr(copied_revision, key) for key in copied_source_revision}
        ) == MediaEditService._content_hash(
            MediaEditService._revision_snapshot(copied_revision)
        )

    revised = _media_service(case).update(
        result.source_session_id,
        2,
        keep_ranges=[{"start_ms": 0, "end_ms": 4_400, "label": "Destination edit"}],
        instructions="Edited independently after copying.",
        reviewed=True,
    )
    assert revised["plan"]["revision"] == 3
    assert revised["plan"]["instructions"] == "Edited independently after copying."

    with case.database.snapshot_session() as session:
        source_plan = session.get(m.MediaEditPlan, original_rows["plan"][0])
        source_revision = session.get(m.MediaEditPlanRevision, original_rows["revision"][0])
        assert (
            source_plan.id,
            source_plan.session_id,
            source_plan.active_revision_id,
        ) == original_rows["plan"]
        assert (
            source_revision.id,
            source_revision.plan_id,
            source_revision.parent_revision_id,
            source_revision.revision_number,
            source_revision.content_hash,
            source_revision.keep_ranges_json,
            source_revision.cues_json,
            source_revision.evidence_json,
            source_revision.operation_json,
            source_revision.instructions,
            source_revision.reviewed,
        ) == original_rows["revision"]
        assert session.get(m.MediaEditPlan, copied_plan_id).active_revision_id != copied_revision_id
        assert session.get(m.MediaEditPlan, copied_plan_id).active_revision_id != original_rows["plan"][2]
    assert source_bytes == {
        media.id: case.services.paths.managed_path(media.relative_path).read_bytes(),
        captions.id: case.services.paths.managed_path(captions.relative_path).read_bytes(),
    }
    assert hashlib.sha256(source_bytes[media.id]).hexdigest() == media.content_hash
    assert hashlib.sha256(source_bytes[captions.id]).hexdigest() == captions.content_hash


def test_materialized_render_and_branch_checkpoint_keep_paired_media_history(publication_case):
    case = publication_case
    source_media, captions, _media_bytes, _caption_bytes = _register_edit_inputs(case)
    service = _media_service(case)
    prepared = service.prepare(case.source.id)
    older_revision_id = prepared["plan"]["revision_id"]
    active = service.update(
        case.source.id,
        1,
        keep_ranges=[{"start_ms": 0, "end_ms": 4_500, "label": "Current edit"}],
        instructions="Keep the current edit.",
        reviewed=True,
    )
    active_revision_id = active["plan"]["revision_id"]
    branch_session_id = case.branches[0]["session_id"]
    with case.database.immediate_session() as session:
        branch_session = session.get(m.SessionRecord, branch_session_id)
        branch_session.workflow_kind = "media_edit"
        source_assets = {
            row.artifact_id: row
            for row in session.scalars(
                select(m.SourceAsset).where(
                    m.SourceAsset.artifact_id.in_([source_media.id, captions.id])
                )
            )
        }
        assert set(source_assets) == {source_media.id, captions.id}
        session.add_all(
            [
                m.SessionSource(
                    session_id=branch_session_id,
                    source_asset_id=source_assets[source_media.id].id,
                    role="primary",
                ),
                m.SessionSource(
                    session_id=branch_session_id,
                    source_asset_id=source_assets[captions.id].id,
                    role="transcript",
                ),
            ]
        )
    branch_prepared = service.prepare(branch_session_id)
    branch_revision_id = branch_prepared["plan"]["revision_id"]

    subtitle_bytes = b"1\n00:00:01,000 --> 00:00:02,500\nKeep this sentence.\n"
    subtitle_path = case.services.paths.root / f"{new_id()}.srt"
    subtitle_path.write_bytes(subtitle_bytes)
    document_hash = hashlib.sha256(subtitle_bytes).hexdigest()
    with case.database.immediate_session() as session:
        document = m.Document(
            session_id=case.source.id,
            stage="media_edit_subtitles",
            language="en",
        )
        session.add(document)
        session.flush()
        document_revision = m.DocumentRevision(
            document_id=document.id,
            revision_number=1,
            content_hash=document_hash,
            reviewed=True,
        )
        session.add(document_revision)
        session.flush()
        document.active_revision_id = document_revision.id
        media_plan = session.scalar(
            select(m.MediaEditPlan).where(m.MediaEditPlan.session_id == case.source.id)
        )
        branch_media_plan = session.scalar(
            select(m.MediaEditPlan).where(m.MediaEditPlan.session_id == branch_session_id)
        )
        old_revision = session.get(m.MediaEditPlanRevision, older_revision_id)
        active_revision = session.get(m.MediaEditPlanRevision, active_revision_id)
        branch_active_revision = session.get(m.MediaEditPlanRevision, branch_revision_id)
        group = session.scalar(
            select(m.TranslationProject).where(
                m.TranslationProject.project_root_id == case.source.project_id
            )
        )
        branch_checkpoint = session.get(
            m.Artifact, case.branches[0]["source_checkpoint_artifact_id"]
        )
        group.source_media_edit_revision_id = old_revision.id
        group.source_media_edit_content_hash = old_revision.content_hash
        branch_checkpoint.metadata_json = {
            **(branch_checkpoint.metadata_json or {}),
            "media_edit_revision_id": old_revision.id,
            "plan_id": media_plan.id,
            "revision": old_revision.revision_number,
            "content_hash": old_revision.content_hash,
        }
        active_metadata = {
            "plan_id": media_plan.id,
            "media_edit_revision_id": active_revision.id,
            "revision_id": active_revision.id,
            "revision": active_revision.revision_number,
            "content_hash": active_revision.content_hash,
            "source_artifact_id": source_media.id,
            "source_content_hash": source_media.content_hash,
        }
        branch_metadata = {
            "plan_id": branch_media_plan.id,
            "media_edit_revision_id": branch_active_revision.id,
            "revision_id": branch_active_revision.id,
            "revision": branch_active_revision.revision_number,
            "content_hash": branch_active_revision.content_hash,
            "source_artifact_id": source_media.id,
            "source_content_hash": source_media.content_hash,
        }
        document_metadata = {
            **active_metadata,
            "revision_id": document_revision.id,
            "document_id": document.id,
        }

    rendered_media_bytes = b"rendered-media-bytes-for-copy"
    rendered_media_path = case.services.paths.root / f"{new_id()}.mp4"
    rendered_media_path.write_bytes(rendered_media_bytes)
    rendered_media = case.services.artifacts.register(
        rendered_media_path,
        kind="video",
        role="media_edit_media",
        session_id=case.source.id,
        parent_ids=[source_media.id, captions.id],
        metadata=active_metadata,
    )
    branch_rendered_media_bytes = b"branch-rendered-media-bytes-for-copy"
    branch_rendered_media_path = case.services.paths.root / f"{new_id()}.mp4"
    branch_rendered_media_path.write_bytes(branch_rendered_media_bytes)
    branch_rendered_media = case.services.artifacts.register(
        branch_rendered_media_path,
        kind="video",
        role="media_edit_media",
        session_id=branch_session_id,
        parent_ids=[source_media.id, captions.id],
        metadata=branch_metadata,
    )
    subtitle = case.services.artifacts.register(
        subtitle_path,
        kind="srt",
        role="media_edit_subtitles",
        session_id=case.source.id,
        parent_ids=[rendered_media.id, captions.id],
        metadata=document_metadata,
    )
    timing_bytes = json.dumps(
        {
            "schema": "pandrator.transcript.v1",
            "metadata": {
                "plan_id": media_plan.id,
                "revision_id": active_revision.id,
                "revision": active_revision.revision_number,
                "timing_method": "media_edit_retime",
            },
            "segments": [
                {
                    "id": "local-cue-1",
                    "start_ms": 1_000,
                    "end_ms": 2_500,
                    "text": "Keep this sentence.",
                    "words": [],
                }
            ],
        },
        ensure_ascii=False,
        indent=2,
    ).encode()
    timing_path = case.services.paths.root / f"{new_id()}.json"
    timing_path.write_bytes(timing_bytes)
    timing_artifact = case.services.artifacts.register(
        timing_path,
        kind="json",
        role="media_edit_word_timestamps",
        session_id=case.source.id,
        parent_ids=[subtitle.id],
        metadata=active_metadata,
    )
    with case.database.immediate_session() as session:
        for artifact in session.scalars(
            select(m.Artifact).where(
                m.Artifact.session_id == case.source.id,
                m.Artifact.role == "correction",
                m.Artifact.state == "current",
            )
        ):
            artifact.state = "stale"

    snapshot = _capture(case)
    state = json.loads(snapshot.state_json)
    rows = state["rows"]
    assert snapshot.ready, snapshot.blockers
    assert {row["id"] for row in rows["media_edit_plan_revisions"]} == {
        older_revision_id,
        active_revision_id,
        branch_revision_id,
    }
    copied_source_old = next(
        row for row in rows["media_edit_plan_revisions"] if row["id"] == older_revision_id
    )
    copied_source_active = next(
        row for row in rows["media_edit_plan_revisions"] if row["id"] == active_revision_id
    )
    copied_source_branch = next(
        row for row in rows["media_edit_plan_revisions"] if row["id"] == branch_revision_id
    )
    assert copied_source_active["parent_revision_id"] == older_revision_id
    assert copied_source_branch["parent_revision_id"] is None
    assert rows["translation_projects"][0]["source_media_edit_revision_id"] == older_revision_id
    assert copied_source_old["content_hash"] == old_revision.content_hash
    assert copied_source_active["content_hash"] == active_revision.content_hash
    assert any(
        row["id"] == document_revision.id for row in rows["document_revisions"]
    )
    local_identity_probe = dict(copied_source_active)
    local_identity_probe["cues_json"] = json.loads(
        json.dumps(local_identity_probe["cues_json"])
    )
    local_identity_probe["cues_json"][0]["id"] = source_media.id
    local_identity_probe["keep_ranges_json"][0]["id"] = source_media.id
    local_identity_probe["operation_json"] = {
        "type": "boundary_refinement",
        "start_cue_id": source_media.id,
        "adjacent_keep_range_id": source_media.id,
    }
    local_identity_probe["content_hash"] = revision_hash(local_identity_probe)
    identity_map = {
        old_id: copy_identity(case.copy_id, table, old_id)
        for table, old_id in (
            ("media_edit_plan_revisions", active_revision_id),
            ("media_edit_plan_revisions", older_revision_id),
            ("media_edit_plan_revisions", branch_revision_id),
            ("media_edit_plans", media_plan.id),
            ("media_edit_plans", branch_media_plan.id),
            ("artifacts", source_media.id),
            ("artifacts", captions.id),
        )
    }
    remapped_probe = remap_revision(
        local_identity_probe,
        identity_map,
        {active_revision_id, older_revision_id},
    )
    assert remapped_probe["source_media_artifact_id"] == identity_map[source_media.id]
    assert remapped_probe["cues_json"][0]["id"] == source_media.id
    assert remapped_probe["keep_ranges_json"][0]["id"] == source_media.id
    assert remapped_probe["operation_json"]["start_cue_id"] == source_media.id
    assert remapped_probe["operation_json"]["adjacent_keep_range_id"] == source_media.id

    case.copy_id = new_id()
    case.snapshot = snapshot
    case.staged = _stage(case, snapshot)
    with case.database.immediate_session() as session:
        result = _publish(session, case, snapshot, case.staged)

    new_old_revision_id = copy_identity(
        case.copy_id, "media_edit_plan_revisions", older_revision_id
    )
    new_active_revision_id = copy_identity(
        case.copy_id, "media_edit_plan_revisions", active_revision_id
    )
    new_document_revision_id = copy_identity(
        case.copy_id, "document_revisions", document_revision.id
    )
    new_plan_id = copy_identity(case.copy_id, "media_edit_plans", media_plan.id)
    new_branch_plan_id = copy_identity(
        case.copy_id, "media_edit_plans", branch_media_plan.id
    )
    new_branch_revision_id = copy_identity(
        case.copy_id, "media_edit_plan_revisions", branch_revision_id
    )
    new_branch_session_id = copy_identity(case.copy_id, "sessions", branch_session_id)
    new_media_id = copy_identity(case.copy_id, "artifacts", rendered_media.id)
    new_branch_media_id = copy_identity(case.copy_id, "artifacts", branch_rendered_media.id)
    new_subtitle_id = copy_identity(case.copy_id, "artifacts", subtitle.id)
    new_timing_id = copy_identity(case.copy_id, "artifacts", timing_artifact.id)
    with case.database.snapshot_session() as session:
        copied_old = session.get(m.MediaEditPlanRevision, new_old_revision_id)
        copied_active = session.get(m.MediaEditPlanRevision, new_active_revision_id)
        copied_group = session.scalar(
            select(m.TranslationProject).where(
                m.TranslationProject.project_root_id == result.project_id
            )
        )
        copied_branch = session.get(
            m.Artifact,
            copy_identity(
                case.copy_id,
                "artifacts",
                case.branches[0]["source_checkpoint_artifact_id"],
            ),
        )
        copied_media = session.get(m.Artifact, new_media_id)
        copied_branch_revision = session.get(m.MediaEditPlanRevision, new_branch_revision_id)
        copied_branch_plan = session.get(m.MediaEditPlan, new_branch_plan_id)
        copied_branch_media = session.get(m.Artifact, new_branch_media_id)
        copied_subtitle = session.get(m.Artifact, new_subtitle_id)
        copied_timing = session.get(m.Artifact, new_timing_id)
        assert copied_old.content_hash != old_revision.content_hash
        assert copied_active.content_hash != active_revision.content_hash
        assert copied_active.parent_revision_id == new_old_revision_id
        assert copied_group.source_media_edit_revision_id == new_old_revision_id
        assert copied_group.source_media_edit_content_hash == copied_old.content_hash
        assert copied_branch.metadata_json["media_edit_revision_id"] == new_old_revision_id
        assert copied_branch.metadata_json["plan_id"] == new_plan_id
        assert copied_branch.metadata_json["content_hash"] == copied_old.content_hash
        for artifact in (copied_media, copied_subtitle, copied_timing):
            assert artifact.metadata_json["media_edit_revision_id"] == new_active_revision_id
            assert artifact.metadata_json["plan_id"] == new_plan_id
            assert artifact.metadata_json["content_hash"] == copied_active.content_hash
        assert copied_subtitle.metadata_json["revision_id"] == new_document_revision_id
        assert copied_subtitle.metadata_json["revision_id"] != new_active_revision_id
        assert copied_branch_plan.active_revision_id == new_branch_revision_id
        assert copied_branch_revision.plan_id == new_branch_plan_id
        assert copied_branch_revision.source_media_artifact_id == copy_identity(
            case.copy_id, "artifacts", source_media.id
        )
        assert copied_branch_media.metadata_json["media_edit_revision_id"] == new_branch_revision_id
        assert copied_branch_media.metadata_json["plan_id"] == new_branch_plan_id
        assert copied_branch_media.metadata_json["content_hash"] == copied_branch_revision.content_hash
        assert copied_media.metadata_json["source_artifact_id"] == copy_identity(
            case.copy_id, "artifacts", source_media.id
        )
        assert copied_media.metadata_json["source_content_hash"] == source_media.content_hash
        assert copied_timing.content_hash == timing_artifact.content_hash
        timing_copy = case.services.paths.managed_path(copied_timing.relative_path)
        assert timing_copy.read_bytes() == timing_bytes
        origin_metadata = json.loads(timing_copy.read_text(encoding="utf-8"))["metadata"]
        assert origin_metadata["plan_id"] == media_plan.id
        assert origin_metadata["revision_id"] == active_revision.id
        assert session.get(m.DocumentRevision, new_document_revision_id) is not None

    copied_state = _media_service(case).state(result.source_session_id)
    assert copied_state["plan"]["revision_id"] == new_active_revision_id
    assert copied_state["plan"]["content_hash"] == copied_active.content_hash
    with case.database.snapshot_session() as session:
        copied_revision = session.get(m.MediaEditPlanRevision, new_active_revision_id)
        media_artifact = session.get(m.Artifact, new_media_id)
        subtitle_artifact = session.get(m.Artifact, new_subtitle_id)
        assert revision_hash(
            {key: getattr(copied_revision, key) for key in REVISION_FIELDS}
        ) == MediaEditService._content_hash(
            MediaEditService._revision_snapshot(copied_revision)
        )
        from pandrator.web.workflow_inputs import _matches_active_media_edit_revision

        assert _matches_active_media_edit_revision(media_artifact, copied_revision)
        assert _matches_active_media_edit_revision(subtitle_artifact, copied_revision)

    export_inputs = resolve_export_inputs(
        _output_context(case),
        {
            "session_id": result.source_session_id,
            "settings": {
                "export_mode": "media",
                "audio_mode": "preserve",
                "subtitle_mode": "burned",
                "subtitle_selection": "source",
            },
            "export_contract": {
                "version": 1,
                "workflow_kind": "media_edit",
                "export_mode": "media",
                "audio_mode": "preserve",
                "source_artifact_id": new_media_id,
                "source_content_hash": copied_media.content_hash,
                "source_profile": "video",
                "source_resolution": "attached",
            },
        },
    )
    export_selection = select_media_export(export_inputs)
    assert export_inputs.active_media_edit_revision is not None
    assert export_selection.upload_media is not None
    assert export_inputs.active_media_edit_revision.id == new_active_revision_id
    assert export_selection.upload_media.id == new_media_id
    assert [item.id for item in export_selection.selected_subtitles] == [new_subtitle_id]

    branch_export_inputs = resolve_export_inputs(
        _output_context(case),
        {
            "session_id": new_branch_session_id,
            "settings": {
                "export_mode": "media",
                "audio_mode": "preserve",
                "subtitle_mode": "burned",
                "subtitle_selection": "source",
            },
            "export_contract": {
                "version": 1,
                "workflow_kind": "media_edit",
                "export_mode": "media",
                "audio_mode": "preserve",
                "source_artifact_id": new_branch_media_id,
                "source_content_hash": copied_branch_media.content_hash,
                "source_profile": "video",
                "source_resolution": "attached",
            },
        },
    )
    branch_export_selection = select_media_export(branch_export_inputs)
    assert branch_export_inputs.active_media_edit_revision is not None
    assert branch_export_selection.upload_media is not None
    assert branch_export_inputs.active_media_edit_revision.id == new_branch_revision_id
    assert branch_export_selection.upload_media.id == new_branch_media_id

    unrelated_render_path = case.services.paths.root / f"{new_id()}.mp4"
    unrelated_render_path.write_bytes(b"unrelated-session-render")
    case.services.artifacts.register(
        unrelated_render_path,
        kind="video",
        role="media_edit_media",
        session_id=case.branches[1]["session_id"],
        parent_ids=[source_media.id, captions.id],
        metadata=branch_metadata,
    )
    with pytest.raises(ProjectCopyError) as failure:
        _capture(case)
    assert failure.value.code == "copy_graph_invalid"


def test_media_pair_headers_use_portable_bind_chunks(publication_case):
    case = publication_case
    _register_edit_inputs(case)
    service = _media_service(case)
    prepared = service.prepare(case.source.id)
    revision_ids = {1: prepared["plan"]["revision_id"]}
    with case.database.immediate_session() as session:
        plan = session.scalar(
            select(m.MediaEditPlan).where(m.MediaEditPlan.session_id == case.source.id)
        )
        base = session.get(m.MediaEditPlanRevision, revision_ids[1])
        base_row = {key: getattr(base, key) for key in REVISION_FIELDS}
        plan_id = plan.id
        content_hash = base.content_hash
        for revision_number in range(2, 503):
            revision_id = new_id()
            revision_ids[revision_number] = revision_id
            session.add(
                m.MediaEditPlanRevision(
                    **{
                        **base_row,
                        "id": revision_id,
                        "revision_number": revision_number,
                        "parent_revision_id": None,
                    }
                )
            )
        session.flush()

    expected_revision_ids = {revision_ids[number] for number in range(1, 502)}
    unreferenced_revision_id = revision_ids[502]
    authorized_headers = {}
    for revision_number in range(1, 502):
        artifact_id = new_id()
        authorized_headers[artifact_id] = {
            "id": artifact_id,
            "session_id": case.source.id,
            "role": "media_edit_media",
            "metadata_json": {
                "plan_id": plan_id,
                "revision": revision_number,
                "content_hash": content_hash,
            },
        }
    sql_statements: list[tuple[str, object]] = []

    def capture_sql(_connection, _cursor, statement, parameters, _context, _many):
        if "media_edit_plan_revisions" in statement:
            sql_statements.append((statement, parameters))

    with case.database.snapshot_session() as session:
        plans = capture_module._media_plans(session, {case.source.id})
        event.listen(case.database.engine, "before_cursor_execute", capture_sql)
        try:
            selected_rows, selected_ids, _dependencies = capture_module._media_selection(
                session,
                {case.source.id},
                plans,
                authorized_headers,
                [],
                {},
            )
        finally:
            event.remove(case.database.engine, "before_cursor_execute", capture_sql)

    media_selects = [
        (statement, parameters)
        for statement, parameters in sql_statements
        if statement.lstrip().upper().startswith("SELECT")
    ]
    body_selects = [
        (statement, parameters)
        for statement, parameters in media_selects
        if "keep_ranges_json" in statement
    ]
    header_selects = [item for item in media_selects if "keep_ranges_json" not in item[0]]
    assert len(header_selects) == 4
    assert len(body_selects) == 2
    assert len(media_selects) == 6

    def values(parameters):
        if isinstance(parameters, dict):
            return list(parameters.values())
        if isinstance(parameters, (tuple, list)):
            return list(parameters)
        return []

    assert all(len(values(parameters)) < 999 for _statement, parameters in media_selects)
    body_bound_ids = {
        value
        for _statement, parameters in body_selects
        for value in values(parameters)
        if value in expected_revision_ids | {unreferenced_revision_id}
    }
    assert body_bound_ids == expected_revision_ids
    assert selected_ids == expected_revision_ids
    assert {row["id"] for row in selected_rows} == expected_revision_ids


@pytest.mark.parametrize(
    "corruption,expected_code",
    [
        ("active_pointer", "copy_graph_invalid"),
        ("foreign_pin", "copy_graph_invalid"),
        ("bad_shape", "copy_graph_invalid"),
        ("hash", "copy_graph_invalid"),
        ("inline_secret", "copy_graph_invalid"),
        ("limit", "copy_limit_exceeded"),
    ],
)
def test_invalid_media_source_state_fails_closed_before_publication(
    publication_case, monkeypatch, corruption, expected_code
):
    case = publication_case
    _media, captions, _media_bytes, _caption_bytes = _register_edit_inputs(case)
    service = _media_service(case)
    service.prepare(case.source.id)
    with case.database.immediate_session() as session:
        plan = session.scalar(
            select(m.MediaEditPlan).where(m.MediaEditPlan.session_id == case.source.id)
        )
        revision = session.get(m.MediaEditPlanRevision, plan.active_revision_id)
        if corruption == "active_pointer":
            plan.active_revision_id = new_id()
        elif corruption == "foreign_pin":
            artifact = session.get(m.Artifact, captions.id)
            artifact.metadata_json = {
                **artifact.metadata_json,
                "media_edit_revision_id": new_id(),
            }
        elif corruption == "bad_shape":
            revision.cues_json = {"not": "a cue list"}
        elif corruption == "hash":
            revision.instructions = "Changed without updating its saved hash."
        elif corruption == "inline_secret":
            revision.evidence_json = {"api_key": "must-not-appear-in-errors"}
            revision.content_hash = MediaEditService._content_hash(
                MediaEditService._revision_snapshot(revision)
            )
    if corruption == "limit":
        monkeypatch.setattr(capture_module, "MAX_MEDIA_EDIT_REVISIONS", 0)
    media_body_queries = []

    def capture_body_sql(_connection, _cursor, statement, _parameters, _context, _many):
        if "keep_ranges_json" in statement:
            media_body_queries.append(statement)

    event.listen(case.database.engine, "before_cursor_execute", capture_body_sql)
    try:
        with pytest.raises(ProjectCopyError) as failure:
            _capture(case)
    finally:
        event.remove(case.database.engine, "before_cursor_execute", capture_body_sql)
    assert failure.value.code == expected_code
    expected_message = (
        "The saved project state is incomplete or inconsistent."
        if expected_code == "copy_graph_invalid"
        else "This project exceeds the current copy limits."
    )
    assert str(failure.value) == expected_message
    if corruption in {"active_pointer", "foreign_pin", "limit"}:
        assert not media_body_queries
    if corruption == "inline_secret":
        assert "must-not-appear-in-errors" not in str(failure.value)


@pytest.mark.parametrize(
    "forged_pair", ["render", "translation", "revision_artifact"]
)
def test_forged_frozen_media_bindings_reject_before_first_insert(publication_case, forged_pair):
    case = publication_case
    media, captions, _media_bytes, _caption_bytes = _register_edit_inputs(case)
    service = _media_service(case)
    prepared = service.prepare(case.source.id)
    revision_id = prepared["plan"]["revision_id"]
    with case.database.immediate_session() as session:
        plan = session.scalar(
            select(m.MediaEditPlan).where(m.MediaEditPlan.session_id == case.source.id)
        )
        revision = session.get(m.MediaEditPlanRevision, revision_id)
        group = session.scalar(
            select(m.TranslationProject).where(
                m.TranslationProject.project_root_id == case.source.project_id
            )
        )
        group.source_media_edit_revision_id = revision.id
        group.source_media_edit_content_hash = revision.content_hash
        metadata = {
            "plan_id": plan.id,
            "media_edit_revision_id": revision.id,
            "revision_id": revision.id,
            "revision": revision.revision_number,
            "content_hash": revision.content_hash,
            "source_artifact_id": media.id,
            "source_content_hash": media.content_hash,
        }
    render_path = case.services.paths.root / f"{new_id()}.mp4"
    render_path.write_bytes(b"materialized media bytes")
    render = case.services.artifacts.register(
        render_path,
        kind="video",
        role="media_edit_media",
        session_id=case.source.id,
        parent_ids=[media.id, captions.id],
        metadata=metadata,
    )
    snapshot = _capture(case)
    assert snapshot.ready, snapshot.blockers
    state = json.loads(snapshot.state_json)
    if forged_pair == "render":
        artifact = next(row for row in state["rows"]["artifacts"] if row["id"] == render.id)
        artifact["metadata_json"]["content_hash"] = "0" * 64
    elif forged_pair == "translation":
        state["rows"]["translation_projects"][0]["source_media_edit_content_hash"] = "0" * 64
    else:
        revision = state["rows"]["media_edit_plan_revisions"][0]
        revision["source_media_artifact_id"] = new_id()
        revision["content_hash"] = revision_hash(revision)
    forged = _snapshot_with_state(snapshot, state)
    case.copy_id = new_id()
    staged = _stage(case, forged)
    writes = []

    def capture_writes(_connection, _cursor, statement, _parameters, _context, _many):
        if statement.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE", "REPLACE")):
            writes.append(statement)

    event.listen(case.database.engine, "before_cursor_execute", capture_writes)
    try:
        with pytest.raises(ProjectCopyError) as failure:
            with case.database.immediate_session() as session:
                _publish(session, case, forged, staged)
    finally:
        event.remove(case.database.engine, "before_cursor_execute", capture_writes)
    assert failure.value.code == "copy_graph_invalid"
    assert not writes
