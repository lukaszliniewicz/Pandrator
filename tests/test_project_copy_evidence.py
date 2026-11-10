"""Retained editorial evidence follows bounded current-project copies."""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import event, select

from pandrator.web import models as m
from pandrator.web.access.contracts import ProjectAction
from pandrator.web.access.session_policy import require_session_access
from pandrator.web.model_base import new_id
from pandrator.web.projects import copy_snapshot as capture_module
from pandrator.web.projects.copy_contracts import COPY_STATE_VERSION, ProjectCopyError
from pandrator.web.projects.copy_evidence import (
    evidence_reference_sessions,
    referenced_evidence_ids,
    validate_evidence_graph,
)
from pandrator.web.projects.copy_publication import copy_identity
from tests.project_copy_test_support import (
    capture_with_connections,
    snapshot_destination_connections,
)
from tests.test_project_copy_publication import (
    _publish,
    _stage,
)
from tests.test_project_copy_publication import (
    publication_case as _publication_case,
)
from tests.test_project_copy_snapshot import _context
from tests.test_project_copy_snapshot import bundle_case as _bundle_case
from tests.test_project_copy_snapshot import copy_case as _copy_case

bundle_case = _bundle_case
copy_case = _copy_case
publication_case = _publication_case


def _artifact(
    case,
    name: str,
    body: bytes,
    *,
    session_id: str,
    kind: str,
    role: str,
    metadata: dict[str, Any] | None = None,
    parent_ids: list[str] | None = None,
):
    suffix = {"srt": ".srt", "wav": ".wav", "json": ".json"}[kind]
    path = case.services.paths.uploads / f"copy-evidence-{name}-{new_id()}{suffix}"
    path.write_bytes(body)
    return case.services.artifacts.register(
        path,
        kind=kind,
        role=role,
        session_id=session_id,
        project_id=case.source.project_id,
        workspace_id=case.source_workspace,
        metadata=metadata or {},
        parent_ids=parent_ids or [],
    )


def _source_pin(case):
    with case.database.immediate_session() as session:
        document = m.Document(session_id=case.source.id, stage="transcription", language="en")
        session.add(document)
        session.flush()
        older = m.DocumentRevision(
            document_id=document.id,
            revision_number=1,
            content_hash="historical-source-revision",
            reviewed=True,
        )
        active = m.DocumentRevision(
            document_id=document.id,
            revision_number=2,
            content_hash="active-source-revision",
        )
        session.add_all([older, active])
        session.flush()
        document.active_revision_id = active.id
        older_segment = m.Segment(
            revision_id=older.id,
            ordinal=0,
            text="Historical pinned cue.",
            start_ms=100,
            end_ms=950,
            metadata_json={},
        )
        active_segment = m.Segment(
            revision_id=active.id,
            ordinal=0,
            text="Current source cue.",
            start_ms=100,
            end_ms=950,
            metadata_json={},
        )
        session.add_all([older_segment, active_segment])
        session.flush()
        result = (older.id, older_segment.id, active.id)
    source = _artifact(
        case,
        "source-subtitles",
        b"1\n00:00:00,100 --> 00:00:00,950\nHistorical pinned cue.\n",
        session_id=case.source.id,
        kind="srt",
        role="transcription",
        metadata={"revision_id": result[0]},
    )
    return (*result, source)


def _candidate(route: str, transcript_id: str, *, candidate_id: str = "same-candidate"):
    base = {
        "id": candidate_id,
        "route": route,
        "status": "success",
        "text": "Selected source words.",
        "context_text": "Selected source words in context.",
        "selection_method": "word_overlap",
        "language": "en",
        "timing_kind": "native_word",
        "timing_method": "provider_word_timing",
        "provider": "Human readable provider label",
        "model": "Display model name",
        "engine": route,
        "compute_backend": "CPU",
        "transcript_artifact_id": transcript_id,
        "cost": {"amount": 9},
        "usage": {"private_runtime_counter": 42},
        "cache": {"key_sha256": "private-cache-key"},
        "reused_from": {"evidence_id": "private-reuse-history"},
        "provider_id": "private-provider-binding",
    }
    if route == "audio_llm":
        base.update(
            segments=[],
            words=[],
            transport={
                "input_contract": "openai_chat_completions.input_audio",
                "provider_wire_mapping": "openai_chat_completions.input_audio",
                "audio_consumption": "confirmed",
                "api_key": "private credential material",
            },
        )
    else:
        word = {
            "text": "Selected",
            "start_ms": 100,
            "end_ms": 350,
            "speaker": None,
            "confidence": 0.98,
            "provider_debug": "omit this nested field",
        }
        base.update(
            segments=[
                {
                    "id": "provider-segment-1",
                    "start_ms": 100,
                    "end_ms": 900,
                    "text": "Selected source words.",
                    "speaker": "Narrator",
                    "words": [word],
                    "provider_debug": "omit this nested field",
                }
            ],
            words=[word],
        )
    return base


def _insert_evidence(
    case,
    *,
    session_id: str,
    source_artifact_id: str,
    source_revision_id: str,
    source_segment_id: str | None,
    route: str = "whisper",
    referenced_by: str = "segment",
    status: str = "resolved",
    evidence_id: str | None = None,
):
    evidence_id = evidence_id or new_id()
    source_media = _artifact(
        case,
        "media",
        b"source audio bytes",
        session_id=session_id,
        kind="wav",
        role="recording",
    )
    clip = _artifact(
        case,
        "clip",
        b"bounded audio clip bytes",
        session_id=session_id,
        kind="wav",
        role="subtitle_evidence_audio",
        metadata={
            "evidence_id": evidence_id,
            "source_media_artifact_id": source_media.id,
            "reused_from_evidence_id": "old-evidence",
            "operational": {"reused_from_transcript_artifact_id": "old-transcript"},
        },
        parent_ids=[source_artifact_id, source_media.id],
    )
    transcript_bytes = json.dumps(
        {"text": "Selected source words.", "provider": "Display label", "usage": {"tokens": 1}},
        sort_keys=True,
    ).encode()
    transcript = _artifact(
        case,
        "transcript",
        transcript_bytes,
        session_id=session_id,
        kind="json",
        role="subtitle_evidence_transcript",
        metadata={
            "evidence_id": evidence_id,
            "route": route,
            "provider": "Artifact display attribution",
            "reused_from_transcript_artifact_id": "old-transcript",
        },
        parent_ids=[source_artifact_id, clip.id],
    )
    candidate = _candidate(route, transcript.id)
    with case.database.immediate_session() as session:
        row = m.SubtitleEvidence(
            id=evidence_id,
            session_id=session_id,
            source_artifact_id=source_artifact_id,
            source_media_artifact_id=source_media.id,
            source_revision_id=source_revision_id,
            source_segment_id=source_segment_id,
            cue_id=1,
            start_ms=100,
            end_ms=950,
            clip_start_ms=0,
            clip_end_ms=1000,
            reason="Retain this human reason.",
            routes_json=[route],
            audio_model_ids_json=["private-audio-model"] if route == "audio_llm" else [],
            status=status,
            clip_artifact_id=clip.id,
            candidates_json=[candidate],
            resolution_json={"action": "accepted", "candidate_id": candidate["id"]}
            if status == "resolved"
            else {},
            error_message="runtime diagnostic to omit",
        )
        session.add(row)
        if referenced_by == "segment" and source_segment_id is not None:
            segment = session.get(m.Segment, source_segment_id)
            segment.metadata_json = {
                **(segment.metadata_json or {}),
                "evidence_ids": [*(segment.metadata_json or {}).get("evidence_ids", []), evidence_id],
            }
        elif referenced_by == "passage":
            artifact = session.get(m.Artifact, source_artifact_id)
            metadata = dict(artifact.metadata_json or {})
            metadata["logical_passages"] = {
                "schema_version": 1,
                "display_revision_id": source_revision_id,
                "display_content_hash": artifact.content_hash,
                "source_artifact_id": source_artifact_id,
                "source_revision_id": source_revision_id,
                "items": [
                    {
                        "id": "branch-passage",
                        "start_ms": 100,
                        "end_ms": 950,
                        "text": "Branch passage.",
                        "evidence_ids": [evidence_id],
                    }
                ],
            }
            artifact.metadata_json = metadata
    return evidence_id, clip.id, transcript.id, transcript_bytes, source_media.id


def _capture_for(case, **kwargs):
    with case.database.snapshot_session() as session:
        return capture_with_connections(
            case,
            session,
            _context(case.principal),
            case.source.project_id,
            case.destination,
            settings_service=case.services.workspace_settings,
            **kwargs,
        )


def test_referenced_evidence_copies_historical_pins_and_pair_scoped_candidates(publication_case):
    case = publication_case
    source_revision, source_segment, active_revision, source_artifact = _source_pin(case)
    source = _insert_evidence(
        case,
        session_id=case.source.id,
        source_artifact_id=source_artifact.id,
        source_revision_id=source_revision,
        source_segment_id=source_segment,
        route="whisper",
        referenced_by="segment",
    )
    branch_sid = case.branches[0]["session_id"]
    branch_revision = case.revisions[0][1]
    branch_segment = case.segments[0][1]
    branch = _insert_evidence(
        case,
        session_id=branch_sid,
        source_artifact_id=case.current.id,
        source_revision_id=branch_revision,
        source_segment_id=branch_segment,
        route="audio_llm",
        referenced_by="passage",
    )
    case.copy_id = new_id()
    snapshot = _capture_for(case)
    state = json.loads(snapshot.state_json)
    evidence_rows = state["rows"]["subtitle_evidence"]

    assert snapshot.ready, snapshot.blockers
    assert state["schema_version"] == COPY_STATE_VERSION
    assert {row["id"] for row in evidence_rows} == {source[0], branch[0]}
    assert all(set(row) == {
        "id", "session_id", "source_artifact_id", "source_media_artifact_id",
        "source_revision_id", "source_segment_id", "cue_id", "start_ms", "end_ms",
        "clip_start_ms", "clip_end_ms", "reason", "routes_json", "status",
        "clip_artifact_id", "candidates_json", "resolution_json",
    } for row in evidence_rows)
    for row in evidence_rows:
        candidate = row["candidates_json"][0]
        assert candidate["id"] == "same-candidate"
        assert candidate["provider"] == "Human readable provider label"
        assert candidate["model"] == "Display model name"
        assert "cache" not in candidate and "reused_from" not in candidate
        assert "cost" not in candidate and "usage" not in candidate
        assert "provider_id" not in candidate
    source_row = next(row for row in evidence_rows if row["id"] == source[0])
    branch_row = next(row for row in evidence_rows if row["id"] == branch[0])
    assert source_row["source_revision_id"] == source_revision != active_revision
    assert source_row["candidates_json"][0]["words"][0]["confidence"] == 0.98
    assert branch_row["candidates_json"][0]["transport"]["audio_consumption"] == "confirmed"
    assert "api_key" not in branch_row["candidates_json"][0]["transport"]
    evidence_artifacts = [
        row
        for row in state["rows"]["artifacts"]
        if row["role"] in {"subtitle_evidence_audio", "subtitle_evidence_transcript"}
    ]
    assert all("reused_from_evidence_id" not in row["metadata_json"] for row in evidence_artifacts)
    assert all(
        "reused_from_transcript_artifact_id"
        not in row["metadata_json"].get("operational", {})
        for row in evidence_artifacts
    )

    source_bytes = {
        item.artifact_id: case.services.paths.managed_path(item.relative_path).read_bytes()
        for item in snapshot.files
    }
    staged = _stage(case, snapshot)
    with case.database.immediate_session() as session:
        result = _publish(session, case, snapshot, staged)
    assert result.project_id == copy_identity(case.copy_id, "projects", case.source.project_id)

    with case.database.snapshot_session() as session:
        copied_evidence = {
            row.id: row
            for row in session.scalars(
                select(m.SubtitleEvidence).where(
                    m.SubtitleEvidence.session_id.in_(
                        [copy_identity(case.copy_id, "sessions", case.source.id),
                         copy_identity(case.copy_id, "sessions", branch_sid)]
                    )
                )
            )
        }
        assert len(copied_evidence) == 2
        mapped_source = copied_evidence[copy_identity(case.copy_id, "subtitle_evidence", source[0])]
        mapped_branch = copied_evidence[copy_identity(case.copy_id, "subtitle_evidence", branch[0])]
        assert mapped_source.source_revision_id == copy_identity(case.copy_id, "document_revisions", source_revision)
        assert mapped_source.job_id is None and mapped_source.audio_model_ids_json == []
        assert mapped_source.error_message is None
        assert mapped_source.candidates_json[0]["id"] != "same-candidate"
        assert mapped_branch.candidates_json[0]["id"] != mapped_source.candidates_json[0]["id"]
        assert mapped_source.resolution_json["candidate_id"] == mapped_source.candidates_json[0]["id"]
        assert mapped_branch.resolution_json["candidate_id"] == mapped_branch.candidates_json[0]["id"]
        assert mapped_source.candidates_json[0]["provider"] == "Human readable provider label"
        assert mapped_branch.candidates_json[0]["transport"]["provider_wire_mapping"] == "openai_chat_completions.input_audio"
        assert mapped_source.created_at is not None and mapped_source.updated_at is not None
        require_session_access(
            session,
            _context(case.member),
            mapped_source.session_id,
            ProjectAction.EDIT,
        )
        require_session_access(
            session,
            _context(case.member),
            mapped_branch.session_id,
            ProjectAction.READ,
        )
    with case.database.immediate_session() as session:
        require_session_access(
            session,
            _context(case.member),
            mapped_source.session_id,
            ProjectAction.EDIT,
        )
        case.services.subtitle_evidence.resolve_in_session(
            session,
            mapped_source.session_id,
            mapped_source.id,
            {"action": "edited", "text": "Destination member decision."},
        )
        edited = session.get(m.SubtitleEvidence, mapped_source.id)
        assert edited.resolution_json == {
            "action": "edited",
            "text": "Destination member decision.",
        }

    for item in snapshot.files:
        copied_id = copy_identity(case.copy_id, "artifacts", item.artifact_id)
        with case.database.snapshot_session() as session:
            copied_artifact = session.get(m.Artifact, copied_id)
            assert copied_artifact is not None
            assert case.services.paths.managed_path(copied_artifact.relative_path).read_bytes() == source_bytes[item.artifact_id]
    with case.database.snapshot_session() as session:
        original = session.get(m.SubtitleEvidence, source[0])
        assert original is not None
        assert original.candidates_json[0]["cache"]["key_sha256"] == "private-cache-key"
        assert session.get(m.Segment, source_segment).metadata_json["evidence_ids"] == [source[0]]


def test_optional_candidate_annotations_normalize_and_publish_without_changing_source(
    publication_case,
):
    case = publication_case
    source_revision, source_segment, _active_revision, source_artifact = _source_pin(case)
    evidence = _insert_evidence(
        case,
        session_id=case.source.id,
        source_artifact_id=source_artifact.id,
        source_revision_id=source_revision,
        source_segment_id=source_segment,
    )
    with case.database.immediate_session() as session:
        row = session.get(m.SubtitleEvidence, evidence[0])
        candidate = dict(row.candidates_json[0])
        candidate["segments"] = [
            {
                "text": "Segment without optional annotations.",
                "start_ms": 100,
                "end_ms": 300,
                "words": [{"text": "word", "start_ms": 120, "end_ms": 180}],
            },
            {"text": "Segment without a word list.", "start_ms": 310, "end_ms": 400},
        ]
        candidate["words"] = [{"text": "flat word", "start_ms": 125, "end_ms": 175}]
        source_candidate = json.loads(json.dumps(candidate))
        row.candidates_json = [candidate]

    case.copy_id = new_id()
    snapshot = _capture_for(case)
    assert snapshot.ready, snapshot.blockers
    state = json.loads(snapshot.state_json)
    projected = state["rows"]["subtitle_evidence"][0]["candidates_json"][0]
    assert projected["segments"][0] == {
        "id": None,
        "start_ms": 100,
        "end_ms": 300,
        "text": "Segment without optional annotations.",
        "speaker": None,
        "words": [
            {
                "text": "word",
                "start_ms": 120,
                "end_ms": 180,
                "speaker": None,
                "confidence": None,
            }
        ],
    }
    assert projected["segments"][1]["id"] is None
    assert projected["segments"][1]["speaker"] is None
    assert projected["segments"][1]["words"] == []
    assert projected["words"][0]["speaker"] is None
    assert projected["words"][0]["confidence"] is None

    staged = _stage(case, snapshot)
    with case.database.immediate_session() as session:
        _publish(session, case, snapshot, staged)
    with case.database.snapshot_session() as session:
        original = session.get(m.SubtitleEvidence, evidence[0])
        copied = session.get(
            m.SubtitleEvidence,
            copy_identity(case.copy_id, "subtitle_evidence", evidence[0]),
        )
        assert original is not None and copied is not None
        assert original.candidates_json == [source_candidate]
        assert copied.candidates_json[0]["segments"] == projected["segments"]
        assert copied.candidates_json[0]["words"][0]["confidence"] is None
        source_transcript = session.get(m.Artifact, evidence[2])
        copied_transcript = session.get(
            m.Artifact, copy_identity(case.copy_id, "artifacts", evidence[2])
        )
        assert source_transcript is not None and copied_transcript is not None
        assert case.services.paths.managed_path(source_transcript.relative_path).read_bytes() == evidence[3]
        assert case.services.paths.managed_path(copied_transcript.relative_path).read_bytes() == evidence[3]


@pytest.mark.parametrize("invalid_packet", [[], "bad", True], ids=["list", "string", "boolean"])
def test_malformed_retained_logical_passages_are_rejected_before_body_or_write(
    publication_case, invalid_packet
):
    case = publication_case
    case.copy_id = new_id()
    baseline = _capture_for(case)
    assert baseline.ready, baseline.blockers
    baseline_rows = json.loads(baseline.state_json)["rows"]
    selected = next(row for row in baseline_rows["artifacts"] if row["id"] == case.current.id)

    malformed_artifact = json.loads(json.dumps(selected))
    malformed_artifact["metadata_json"]["logical_passages"] = invalid_packet
    with pytest.raises(ValueError):
        referenced_evidence_ids([], [malformed_artifact])
    with pytest.raises(ValueError):
        evidence_reference_sessions([], [malformed_artifact], {})

    malformed_rows = json.loads(json.dumps(baseline_rows))
    malformed_selected = next(
        row for row in malformed_rows["artifacts"] if row["id"] == case.current.id
    )
    malformed_selected["metadata_json"]["logical_passages"] = invalid_packet
    with pytest.raises(ValueError):
        validate_evidence_graph(malformed_rows)

    with case.database.immediate_session() as session:
        artifact = session.get(m.Artifact, case.current.id)
        metadata = dict(artifact.metadata_json or {})
        metadata["logical_passages"] = invalid_packet
        artifact.metadata_json = metadata

    evidence_body_reads: list[str] = []
    inserts: list[str] = []

    def record(_connection, _cursor, statement, _parameters, _context, _many):
        normalized = statement.lower().lstrip()
        if normalized.startswith("insert"):
            inserts.append(normalized)
        if normalized.startswith("select") and "subtitle_evidence" in normalized and any(
            field in normalized for field in ("candidates_json", "resolution_json")
        ):
            evidence_body_reads.append(normalized)

    event.listen(case.database.engine, "before_cursor_execute", record)
    try:
        with pytest.raises(ProjectCopyError) as failure:
            _capture_for(case)
    finally:
        event.remove(case.database.engine, "before_cursor_execute", record)

    assert failure.value.code == "copy_graph_invalid"
    assert evidence_body_reads == []
    assert inserts == []


def test_unreferenced_active_evidence_and_files_are_omitted_and_active_reference_is_body_free(
    publication_case,
):
    case = publication_case
    revision = case.revisions[0][1]
    segment_id = case.segments[0][1]
    inactive = _insert_evidence(
        case,
        session_id=case.branches[0]["session_id"],
        source_artifact_id=case.current.id,
        source_revision_id=revision,
        source_segment_id=None,
        status="completed",
        referenced_by="none",
    )
    active = _insert_evidence(
        case,
        session_id=case.branches[0]["session_id"],
        source_artifact_id=case.current.id,
        source_revision_id=revision,
        source_segment_id=None,
        status="running",
        referenced_by="none",
    )
    with case.database.immediate_session() as session:
        row = session.get(m.SubtitleEvidence, inactive[0])
        row.candidates_json = [{"runtime_private_body": "never fetched for unreferenced evidence"}]

    case.copy_id = new_id()
    unreferenced = _capture_for(case)
    unreferenced_rows = json.loads(unreferenced.state_json)
    copied_artifacts = {row["id"] for row in unreferenced_rows["rows"]["artifacts"]}
    assert unreferenced.ready
    assert unreferenced_rows["rows"]["subtitle_evidence"] == []
    assert not {inactive[1], inactive[2], active[1], active[2]} & copied_artifacts
    destination_connections = snapshot_destination_connections(unreferenced)

    with case.database.immediate_session() as session:
        segment = session.get(m.Segment, segment_id)
        segment.metadata_json = {**segment.metadata_json, "evidence_ids": [active[0]]}
    evidence_sql: list[str] = []

    def record(_connection, _cursor, statement, _parameters, _context, _many):
        normalized = statement.lower()
        if normalized.lstrip().startswith("select") and "subtitle_evidence" in normalized:
            evidence_sql.append(normalized)

    event.listen(case.database.engine, "before_cursor_execute", record)
    try:
        blocked = _capture_for(case, destination_connections=destination_connections)
    finally:
        event.remove(case.database.engine, "before_cursor_execute", record)

    assert not blocked.ready
    assert ("unsupported_state", "active_evidence") in blocked.blockers
    assert len(evidence_sql) == 1
    assert "candidates_json" not in evidence_sql[0]
    assert "resolution_json" not in evidence_sql[0]


@pytest.mark.parametrize(
    "invalid_reference",
    ["dangling", "malformed", "foreign_session"],
)
def test_invalid_reference_scope_is_rejected_before_body_hydration(publication_case, invalid_reference):
    case = publication_case
    if invalid_reference == "foreign_session":
        revision = case.revisions[1][1]
        segment_id = case.segments[0][1]
        evidence = _insert_evidence(
            case,
            session_id=case.branches[1]["session_id"],
            source_artifact_id=case.old_unselected[1],
            source_revision_id=revision,
            source_segment_id=case.segments[1][1],
            referenced_by="none",
        )
        reference: Any = [evidence[0]]
    elif invalid_reference == "malformed":
        segment_id = case.segments[0][1]
        reference = [1]
    else:
        segment_id = case.segments[0][1]
        reference = [new_id()]
    with case.database.immediate_session() as session:
        segment = session.get(m.Segment, segment_id)
        segment.metadata_json = {**segment.metadata_json, "evidence_ids": reference}

    with pytest.raises(ProjectCopyError) as failure:
        _capture_for(case)
    assert failure.value.code == "copy_graph_invalid"


def test_capture_is_deterministic_and_ignores_runtime_cache_fields(publication_case, monkeypatch):
    case = publication_case
    revision = case.revisions[0][1]
    item = _insert_evidence(
        case,
        session_id=case.branches[0]["session_id"],
        source_artifact_id=case.current.id,
        source_revision_id=revision,
        source_segment_id=case.segments[0][1],
    )
    case.copy_id = new_id()
    before = _capture_for(case)
    assert before.fingerprint == _capture_for(case).fingerprint
    source_files = {
        artifact_id: case.services.paths.managed_path(relative_path).read_bytes()
        for artifact_id in (item[1], item[2])
        for relative_path in [
            next(file.relative_path for file in before.files if file.artifact_id == artifact_id)
        ]
    }

    def deny_file_access(*_args, **_kwargs):
        raise AssertionError("Copy capture must not read or write artifact files.")

    with monkeypatch.context() as patcher:
        patcher.setattr(Path, "open", deny_file_access)
        repeated = _capture_for(case)
    assert repeated.fingerprint == before.fingerprint
    with case.database.immediate_session() as session:
        row = session.get(m.SubtitleEvidence, item[0])
        candidate = dict(row.candidates_json[0])
        candidate.update(
            cache={"key_sha256": "changed runtime cache"},
            cost={"amount": 99},
            reused_from={"candidate_id": "changed reuse history"},
            usage={"tokens": 999},
        )
        row.candidates_json = [candidate]
    after_runtime = _capture_for(case)
    assert after_runtime.fingerprint == before.fingerprint
    for artifact_id, body in source_files.items():
        file_row = next(file for file in before.files if file.artifact_id == artifact_id)
        assert case.services.paths.managed_path(file_row.relative_path).read_bytes() == body
    with case.database.immediate_session() as session:
        row = session.get(m.SubtitleEvidence, item[0])
        candidate = dict(row.candidates_json[0])
        candidate["text"] = "Editorial evidence changed."
        row.candidates_json = [candidate]
    assert _capture_for(case).fingerprint != before.fingerprint


@pytest.mark.parametrize("invalid", ["candidate", "resolution", "pin", "deleted_dependency"])
def test_invalid_saved_candidate_resolution_and_pins_are_rejected(publication_case, invalid):
    case = publication_case
    source_revision, source_segment, active_revision, source_artifact = _source_pin(case)
    evidence = _insert_evidence(
        case,
        session_id=case.source.id,
        source_artifact_id=source_artifact.id,
        source_revision_id=source_revision,
        source_segment_id=source_segment,
    )
    with case.database.immediate_session() as session:
        row = session.get(m.SubtitleEvidence, evidence[0])
        if invalid == "candidate":
            row.candidates_json = [
                {
                    "id": "candidate",
                    "route": "whisper",
                    "status": "success",
                    "text": "bad",
                    "segments": [{"id": "segment", "start_ms": 0}],
                }
            ]
        elif invalid == "resolution":
            row.resolution_json = {"action": "accepted", "candidate_id": "missing-candidate"}
        elif invalid == "pin":
            row.source_revision_id = active_revision
        else:
            session.get(m.Artifact, source_artifact.id).state = "deleted"

    with pytest.raises(ProjectCopyError) as failure:
        _capture_for(case)
    assert failure.value.code == "copy_graph_invalid"


def test_evidence_limit_is_applied_to_distinct_retained_references(publication_case, monkeypatch):
    case = publication_case
    source_revision, source_segment, _active_revision, source_artifact = _source_pin(case)
    _insert_evidence(
        case,
        session_id=case.source.id,
        source_artifact_id=source_artifact.id,
        source_revision_id=source_revision,
        source_segment_id=source_segment,
    )
    _insert_evidence(
        case,
        session_id=case.branches[0]["session_id"],
        source_artifact_id=case.current.id,
        source_revision_id=case.revisions[0][1],
        source_segment_id=case.segments[0][1],
    )
    monkeypatch.setattr(capture_module, "MAX_SUBTITLE_EVIDENCE_ROWS", 1)

    with pytest.raises(ProjectCopyError) as failure:
        _capture_for(case)
    assert failure.value.code == "copy_limit_exceeded"


def test_fingerprinted_snapshot_with_unprojected_evidence_fails_before_insert(publication_case):
    case = publication_case
    source_revision, source_segment, _active_revision, source_artifact = _source_pin(case)
    item = _insert_evidence(
        case,
        session_id=case.source.id,
        source_artifact_id=source_artifact.id,
        source_revision_id=source_revision,
        source_segment_id=source_segment,
    )
    case.copy_id = new_id()
    snapshot = _capture_for(case)
    staged = _stage(case, snapshot)
    state = json.loads(snapshot.state_json)
    evidence_row = next(row for row in state["rows"]["subtitle_evidence"] if row["id"] == item[0])
    evidence_row["candidates_json"][0]["cache"] = {"key_sha256": "forged"}
    encoded = json.dumps(state, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    forged = replace(
        snapshot,
        state_json=encoded,
        fingerprint=hashlib.sha256(encoded.encode()).hexdigest(),
    )
    forged_staged = replace(staged, snapshot_fingerprint=forged.fingerprint)
    inserts: list[str] = []

    def record_insert(_connection, _cursor, statement, _parameters, _context, _many):
        if statement.lstrip().lower().startswith("insert"):
            inserts.append(statement)

    event.listen(case.database.engine, "before_cursor_execute", record_insert)
    try:
        with case.database.immediate_session() as session:
            with pytest.raises(ProjectCopyError) as failure:
                _publish(session, case, forged, forged_staged)
    finally:
        event.remove(case.database.engine, "before_cursor_execute", record_insert)

    assert failure.value.code == "copy_graph_invalid"
    assert inserts == []
