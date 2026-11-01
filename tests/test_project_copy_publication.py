"""Caller-owned publication preserves the captured graph and rollback boundary."""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import FrozenInstanceError, replace
from pathlib import Path
from typing import Any, cast
from unittest.mock import patch

import pytest
from sqlalchemy import event, func, select

from pandrator.web import models as m
from pandrator.web.access.artifact_policy import visible_artifact_ids_statement
from pandrator.web.access.contracts import ProjectAction
from pandrator.web.access.project_policy import require_project_access, require_project_creation
from pandrator.web.access.session_policy import (
    require_session_access,
    visible_session_ids_statement,
)
from pandrator.web.accounts.models import Workspace, WorkspaceMembership
from pandrator.web.model_base import new_id
from pandrator.web.projects.copy_contracts import ProjectCopyError
from pandrator.web.projects.copy_files import stage_project_files
from pandrator.web.projects.copy_publication import copy_identity, publish_project_copy_rows
from tests.project_copy_test_support import capture_with_connections, seed_copy_model
from tests.test_project_copy_snapshot import _context, _prepare_copy_case
from tests.test_project_copy_snapshot import bundle_case as _bundle_case
from tests.test_project_copy_snapshot import unbound_copy_case as _copy_case
from tests.test_project_operation_access import _person

bundle_case = _bundle_case
copy_case = _copy_case


def _capture(case):
    with case.database.snapshot_session() as session:
        return capture_with_connections(
            case,
            session,
            _context(case.principal),
            case.source.project_id,
            case.destination,
            name="Independent subtitle copy",
            settings_service=case.services.workspace_settings,
        )


def _stage(case, snapshot=None):
    snapshot = snapshot or case.snapshot
    return stage_project_files(
        case.services.paths,
        snapshot,
        copy_id=case.copy_id,
        artifact_ids={
            file.artifact_id: copy_identity(case.copy_id, "artifacts", file.artifact_id)
            for file in snapshot.files
        },
        check=lambda: None,
    )


@pytest.fixture
def publication_case(bundle_case):
    case = _prepare_copy_case(bundle_case)
    with case.database.immediate_session() as session:
        destination = Workspace(id=new_id(), kind="team", name="Publication team")
        session.add(destination)
        session.flush()
        session.add(
            WorkspaceMembership(
                workspace_id=destination.id, user_id=case.principal.user_id, role="owner"
            )
        )
        case.destination = destination.id
        artifact = session.get(m.Artifact, case.current.id)
        artifact.metadata_json = {
            **artifact.metadata_json,
            "source_artifact_id": case.used_library.id,
            "source_content_hash": case.used_library.content_hash,
            "session_id": case.branches[0]["session_id"],
            "document_id": case.documents[0],
            "segment_ids": case.segments[0][:2],
            "source_asset_id": case.used_asset,
            "related_artifact_ids": [case.selected.id, case.used_library.id],
            "by_artifact_id": {
                case.selected.id: {"id": case.selected.id, "text": f"Human text {case.selected.id}"}
            },
            "prompt": f"Retain https://reference.invalid/book and {case.current.id}",
            "job_id": new_id(),
            "generation_run_id": new_id(),
            "source_path": "/private/server/file",
            "raw_paths": ["/private/server/another"],
            "base_url": "https://private-service.invalid/v1",
            "services": {"private": {"base_url": "https://private-service.invalid/v1"}},
        }
        session.add(
            m.StoredCredential(
                workspace_id=case.source_workspace,
                key="copy-private-test",
                label="Private test",
                secret_value="never-copy-credential",
            )
        )
        session.add(m.Voice(workspace_id=case.source_workspace, name="Unused private voice"))
        session.merge(
            m.WorkspaceSetting(
                workspace_id=case.source_workspace,
                key="defaults.subtitles",
                value_json={"max_chars_per_line": 32},
            )
        )
    seed_copy_model(case, case.destination)
    case.member = _person(case, workspace_id=case.destination)
    with case.database.immediate_session() as session:
        session.add(
            WorkspaceMembership(
                workspace_id=case.destination, user_id=case.member.user_id, role="member"
            )
        )
    case.copy_id = new_id()
    case.snapshot = _capture(case)
    assert case.snapshot.ready, case.snapshot.blockers
    case.staged = _stage(case)
    yield case


def _publish(session, case, snapshot=None, staged=None, created_by=None):
    return publish_project_copy_rows(
        session,
        snapshot or case.snapshot,
        staged or case.staged,
        copy_id=case.copy_id,
        created_by_user_id=created_by or case.principal.user_id,
    )


def _authorize_caller(session, case):
    context = _context(case.principal)
    require_project_access(session, context, case.snapshot.source_project_id, ProjectAction.EDIT)
    require_project_creation(session, context, case.snapshot.destination_workspace_id)
    retained = {file.artifact_id for file in case.snapshot.files}
    assert (
        set(
            session.scalars(
                visible_artifact_ids_statement(session, context).where(m.Artifact.id.in_(retained))
            )
        )
        == retained
    )


def _counts(case):
    with case.database.snapshot_session() as session:
        return {
            table: session.scalar(select(func.count()).select_from(m.Base.metadata.tables[table]))
            for table in (
                "jobs",
                "workflow_runs",
                "workflow_execution_plans",
                "api_idempotency",
                "project_folder_memberships",
                "project_memberships",
                "workspace_memberships",
                "stored_credentials",
                "workspace_settings",
                "voices",
            )
        }


def test_real_capture_staging_and_publication_preserve_full_editable_graph(publication_case):
    case = publication_case
    before = _counts(case)
    source_state = case.snapshot.state_json
    with case.database.immediate_session() as session:
        _authorize_caller(session, case)
        result = _publish(session, case)
    assert result.project_id == copy_identity(case.copy_id, "projects", case.source.project_id)
    assert result.source_session_id == copy_identity(case.copy_id, "sessions", case.source.id)
    assert result.translation_project_id == copy_identity(
        case.copy_id, "translation_projects", case.project["id"]
    )
    assert result.workspace_id == case.destination and result.name == case.snapshot.name
    assert result.fingerprint == case.snapshot.fingerprint
    assert set(result.manifest()) == {
        "project_id",
        "source_session_id",
        "translation_project_id",
        "workspace_id",
        "name",
        "fingerprint",
    }
    with pytest.raises(FrozenInstanceError):
        cast(Any, result).name = "changed"
    original = json.loads(source_state)["rows"]
    with case.database.snapshot_session() as session:
        project = session.get(m.Project, result.project_id)
        assert project.source_session_id == result.source_session_id and project.revision == 1
        assert project.created_by_user_id == case.principal.user_id
        sessions = list(
            session.scalars(
                select(m.SessionRecord).where(m.SessionRecord.project_id == result.project_id)
            )
        )
        assert len(sessions) == 3
        assert all(
            row.storage_key == row.id
            and row.status == "idle"
            and row.revision == 1
            and row.legacy_path is None
            and row.trashed_at is None
            for row in sessions
        )
        assert session.get(m.SessionRecord, result.source_session_id).name == case.snapshot.name
        for branch in case.branches:
            assert session.get(
                m.SessionRecord, copy_identity(case.copy_id, "sessions", branch["session_id"])
            ).name == next(
                row["name"] for row in original["sessions"] if row["id"] == branch["session_id"]
            )
        for document, revisions, segments, word in zip(
            case.documents, case.revisions, case.segments, case.words, strict=True
        ):
            copied_document = session.get(
                m.Document, copy_identity(case.copy_id, "documents", document)
            )
            assert copied_document.active_revision_id == copy_identity(
                case.copy_id, "document_revisions", revisions[1]
            )
            old = session.get(
                m.DocumentRevision, copy_identity(case.copy_id, "document_revisions", revisions[0])
            )
            current = session.get(
                m.DocumentRevision, copy_identity(case.copy_id, "document_revisions", revisions[1])
            )
            assert old.parent_revision_id is None
            assert current.parent_revision_id == old.id and current.reviewed is True
            assert (
                session.get(
                    m.DocumentRevision,
                    copy_identity(case.copy_id, "document_revisions", revisions[2]),
                )
                is None
            )
            segment = session.get(m.Segment, copy_identity(case.copy_id, "segments", segments[1]))
            assert (
                segment.text.startswith("Saved text")
                and segment.start_ms == 100
                and segment.end_ms == 950
            )
            timed = session.get(m.TimedWord, copy_identity(case.copy_id, "timed_words", word))
            assert (
                timed.segment_id == segment.id
                and timed.revision_id == current.id
                and timed.confidence == 0.95
            )
            assert (
                session.get(
                    m.SegmentLineage,
                    (copy_identity(case.copy_id, "segments", segments[0]), segment.id),
                )
                is not None
            )
        selected = session.get(
            m.SessionStageSelection,
            (copy_identity(case.copy_id, "sessions", case.branches[0]["session_id"]), "translate"),
        )
        assert (
            selected.artifact_id == copy_identity(case.copy_id, "artifacts", case.selected.id)
            and selected.revision == 1
        )
        assert session.get(m.Artifact, selected.artifact_id).state == "stale"
        artifacts = list(
            session.scalars(select(m.Artifact).where(m.Artifact.project_id == result.project_id))
        )
        assert len(artifacts) == len(case.snapshot.files)
        assert all(row.workspace_id == case.destination for row in artifacts)
        library = session.get(
            m.Artifact, copy_identity(case.copy_id, "artifacts", case.used_library.id)
        )
        assert library.session_id is None and library.project_id == result.project_id
        assert (
            session.get(
                m.Artifact, copy_identity(case.copy_id, "artifacts", case.unused_library.id)
            )
            is None
        )
        asset = session.get(
            m.SourceAsset, copy_identity(case.copy_id, "source_assets", case.used_asset)
        )
        assert (
            asset.workspace_id == case.destination
            and asset.artifact_id == library.id
            and asset.external_path is None
        )
        assert (
            session.get(
                m.SourceAsset, copy_identity(case.copy_id, "source_assets", case.unused_asset)
            )
            is None
        )
        source_records = list(
            session.scalars(
                select(m.SourceRecord).where(m.SourceRecord.session_id == result.source_session_id)
            )
        )
        assert (
            len(source_records) == 1
            and source_records[0].artifact_id == library.id
            and source_records[0].external_path is None
        )
        group = session.get(m.TranslationProject, result.translation_project_id)
        assert (
            group.name == case.snapshot.name
            and group.revision == 1
            and group.source_session_id == result.source_session_id
        )
        for row in original["translation_project_branches"]:
            copied = session.get(
                m.TranslationProjectBranch,
                copy_identity(case.copy_id, "translation_project_branches", row["id"]),
            )
            assert copied.project_id == group.id and copied.session_id == copy_identity(
                case.copy_id, "sessions", row["session_id"]
            )
            assert copied.source_checkpoint_artifact_id == copy_identity(
                case.copy_id, "artifacts", row["source_checkpoint_artifact_id"]
            )
        metadata = session.get(
            m.Artifact, copy_identity(case.copy_id, "artifacts", case.current.id)
        ).metadata_json
        assert metadata["source_artifact_id"] == library.id
        assert metadata["session_id"] == copy_identity(
            case.copy_id, "sessions", case.branches[0]["session_id"]
        )
        assert metadata["document_id"] == copy_identity(
            case.copy_id, "documents", case.documents[0]
        )
        assert metadata["segment_ids"] == [
            copy_identity(case.copy_id, "segments", old) for old in case.segments[0][:2]
        ]
        assert metadata["source_asset_id"] == asset.id
        key = copy_identity(case.copy_id, "artifacts", case.selected.id)
        assert metadata["by_artifact_id"][key] == {
            "id": key,
            "text": f"Human text {case.selected.id}",
        }
        assert metadata["prompt"] == f"Retain https://reference.invalid/book and {case.current.id}"
        assert not {
            "job_id",
            "generation_run_id",
            "source_path",
            "raw_paths",
            "base_url",
            "services",
        }.intersection(metadata)
        for file in case.staged.files:
            copied = session.get(m.Artifact, file.artifact_id)
            assert (
                copied.relative_path == file.relative_path
                and copied.content_hash == file.content_hash
                and copied.size_bytes == file.size_bytes
            )
            path = case.services.paths.managed_path(copied.relative_path)
            assert hashlib.sha256(path.read_bytes()).hexdigest() == file.content_hash
        for row in original["session_settings"]:
            copied = session.get(
                m.SessionSetting,
                (copy_identity(case.copy_id, "sessions", row["session_id"]), row["section"]),
            )
            assert copied.revision == 1
        assert (
            session.get(m.SessionSetting, (result.source_session_id, "subtitles")).value_json[
                "max_chars_per_line"
            ]
            == 32
        )
    assert _counts(case) == before
    assert _capture(case).state_json == source_state


def test_destination_member_discovers_and_edits_without_project_grant(publication_case):
    case = publication_case
    with case.database.immediate_session() as session:
        _authorize_caller(session, case)
        result = _publish(session, case)
    member = _context(case.member)
    copied_segment_id = copy_identity(case.copy_id, "segments", case.segments[0][1])
    with case.database.immediate_session() as session:
        assert (
            session.scalar(
                select(m.ProjectMembership).where(
                    m.ProjectMembership.project_id == result.project_id
                )
            )
            is None
        )
        assert result.source_session_id in set(
            session.scalars(visible_session_ids_statement(session, member))
        )
        require_session_access(
            session,
            member,
            copy_identity(case.copy_id, "sessions", case.branches[0]["session_id"]),
            ProjectAction.EDIT,
        )
        session.get(m.Segment, copied_segment_id).text = "An independent member edit."
    with case.database.snapshot_session() as session:
        assert session.get(m.Segment, case.segments[0][1]).text != "An independent member edit."
        assert session.get(m.Segment, copied_segment_id).text == "An independent member edit."


def test_caller_rollback_removes_all_rows_and_leaves_verified_staged_bytes(publication_case):
    case = publication_case
    root = copy_identity(case.copy_id, "projects", case.source.project_id)
    old_counts = _counts(case)

    class Rollback(Exception):
        pass

    with pytest.raises(Rollback):
        with case.database.immediate_session() as session:
            _authorize_caller(session, case)
            _publish(session, case)
            assert session.get(m.Project, root) is not None
            raise Rollback()
    with case.database.snapshot_session() as session:
        assert session.get(m.Project, root) is None
        for table in (
            "sessions",
            "documents",
            "document_revisions",
            "segments",
            "timed_words",
            "artifacts",
            "source_assets",
            "translation_projects",
        ):
            model = m.Base.metadata.tables[table]
            old_ids = [row["id"] for row in json.loads(case.snapshot.state_json)["rows"][table]]
            mapped = [copy_identity(case.copy_id, table, old) for old in old_ids]
            assert not list(session.scalars(select(model.c.id).where(model.c.id.in_(mapped))))
    assert _counts(case) == old_counts
    assert all(
        case.services.paths.managed_path(file.relative_path).is_file() for file in case.staged.files
    )


def test_publisher_does_no_file_io_and_does_not_commit_or_rollback(publication_case):
    case = publication_case
    with case.database.immediate_session() as session:
        _authorize_caller(session, case)
        with (
            patch.object(session, "commit", side_effect=AssertionError("No internal commit")),
            patch.object(session, "rollback", side_effect=AssertionError("No internal rollback")),
            patch.object(Path, "stat", side_effect=AssertionError("No stat")),
            patch.object(Path, "open", side_effect=AssertionError("No file open")),
            patch("builtins.open", side_effect=AssertionError("No file IO")),
        ):
            result = _publish(session, case)
    assert result.project_id


def test_uppercase_source_file_hashes_publish_canonical_pinned_hashes(publication_case):
    case = publication_case
    state = json.loads(case.snapshot.state_json)
    with case.database.immediate_session() as session:
        for row in state["rows"]["artifacts"]:
            session.get(m.Artifact, row["id"]).content_hash = row["content_hash"].upper()
        group = session.get(m.TranslationProject, case.project["id"])
        group.source_content_hash = group.source_content_hash.upper()
        for branch in case.branches:
            row = session.get(m.TranslationProjectBranch, branch["id"])
            row.source_content_hash = row.source_content_hash.upper()
        asset = session.get(m.SourceAsset, case.used_asset)
        asset.content_hash = case.used_library.content_hash.upper()
        source = session.scalar(
            select(m.SourceRecord).where(m.SourceRecord.session_id == case.source.id)
        )
        source.content_hash = case.used_library.content_hash.upper()
        artifact = session.get(m.Artifact, case.current.id)
        artifact.metadata_json = {
            **artifact.metadata_json,
            "source_content_hash": case.used_library.content_hash.upper(),
            "literal": f"Human hash {case.used_library.content_hash.upper()}",
        }
    snapshot = _capture(case)
    staged = _stage(case, snapshot)
    with case.database.immediate_session() as session:
        result = _publish(session, case, snapshot, staged)
    with case.database.snapshot_session() as session:
        group = session.get(m.TranslationProject, result.translation_project_id)
        assert (
            group.source_content_hash
            == session.get(m.Artifact, group.checkpoint_artifact_id).content_hash
        )
        for branch in session.scalars(
            select(m.TranslationProjectBranch).where(
                m.TranslationProjectBranch.project_id == group.id
            )
        ):
            assert (
                branch.source_content_hash
                == session.get(m.Artifact, branch.source_checkpoint_artifact_id).content_hash
            )
        asset = session.get(
            m.SourceAsset, copy_identity(case.copy_id, "source_assets", case.used_asset)
        )
        assert asset.content_hash == case.used_library.content_hash.lower()
        source = session.scalar(
            select(m.SourceRecord).where(m.SourceRecord.session_id == result.source_session_id)
        )
        assert source.content_hash == asset.content_hash
        artifact = session.get(
            m.Artifact, copy_identity(case.copy_id, "artifacts", case.current.id)
        )
        assert artifact.metadata_json["source_content_hash"] == asset.content_hash
        assert (
            artifact.metadata_json["literal"]
            == f"Human hash {case.used_library.content_hash.upper()}"
        )
        revision = session.get(
            m.DocumentRevision,
            copy_identity(case.copy_id, "document_revisions", case.revisions[0][1]),
        )
        assert (
            revision.content_hash
            == state["rows"]["document_revisions"][
                next(
                    i
                    for i, row in enumerate(state["rows"]["document_revisions"])
                    if row["id"] == case.revisions[0][1]
                )
            ]["content_hash"]
        )


def _changed(case, mutate):
    state = json.loads(case.snapshot.state_json)
    mutate(state)
    encoded = json.dumps(
        state, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False
    )
    fingerprint = hashlib.sha256(encoded.encode()).hexdigest()
    return replace(case.snapshot, state_json=encoded, fingerprint=fingerprint), replace(
        case.staged, snapshot_fingerprint=fingerprint
    )


@pytest.mark.parametrize(
    "change",
    [
        "blocked",
        "digest",
        "unknown_table",
        "unknown_field",
        "duplicate_id",
        "missing_artifact",
        "ownership",
        "unknown_json_reference",
        "media_revision",
        "missing_section",
        "wrong_name",
        "creator",
        "root_collision",
        "stage_fingerprint",
        "missing_file",
        "wrong_mapped_id",
        "hash",
        "size",
        "absolute_path",
        "traversal",
        "outside_attempt",
    ],
)
def test_invalid_graph_or_staging_rejects_before_any_insert(publication_case, change):
    case = publication_case
    snapshot, staged = case.snapshot, case.staged
    creator = None
    if change == "blocked":
        snapshot = replace(snapshot, blockers=(("unsupported_state", "generation_plans"),))
    elif change == "digest":
        snapshot = replace(snapshot, fingerprint="0" * 64)
    elif change == "unknown_table":
        snapshot, staged = _changed(case, lambda state: state["rows"].update({"sound_items": []}))
    elif change == "unknown_field":
        snapshot, staged = _changed(
            case, lambda state: state["rows"]["projects"][0].update({"revision": 4})
        )
    elif change == "duplicate_id":
        snapshot, staged = _changed(
            case, lambda state: state["rows"]["sessions"].append(dict(state["rows"]["sessions"][0]))
        )
    elif change == "missing_artifact":
        snapshot, staged = _changed(case, lambda state: state["rows"]["artifacts"].pop())
    elif change == "ownership":
        snapshot, staged = _changed(
            case, lambda state: state["rows"]["sessions"][0].update({"project_id": new_id()})
        )
    elif change == "unknown_json_reference":
        snapshot, staged = _changed(
            case,
            lambda state: state["rows"]["artifacts"][0]["metadata_json"].update(
                {"source_artifact_id": new_id()}
            ),
        )
    elif change == "media_revision":
        snapshot, staged = _changed(
            case,
            lambda state: state["rows"]["translation_projects"][0].update(
                {"source_media_edit_revision_id": new_id()}
            ),
        )
    elif change == "missing_section":
        snapshot, staged = _changed(case, lambda state: state["rows"]["session_settings"].pop())
    elif change == "wrong_name":
        snapshot = replace(snapshot, name="  Not trimmed  ")
    elif change == "creator":
        creator = new_id()
    elif change == "root_collision":
        with case.database.immediate_session() as session:
            session.add(
                m.Project(
                    id=copy_identity(case.copy_id, "projects", case.source.project_id),
                    workspace_id=case.destination,
                    name="Existing target",
                    created_by_user_id=case.principal.user_id,
                )
            )
    elif change == "stage_fingerprint":
        staged = replace(staged, snapshot_fingerprint="0" * 64)
    elif change == "missing_file":
        staged = replace(staged, files=staged.files[:-1])
    else:
        file = staged.files[0]
        if change == "wrong_mapped_id":
            file = replace(file, artifact_id=new_id())
        elif change == "hash":
            file = replace(file, content_hash="0" * 64)
        elif change == "size":
            file = replace(file, size_bytes=file.size_bytes + 1)
        elif change == "absolute_path":
            file = replace(file, relative_path="/outside/attempt.srt")
        elif change == "traversal":
            file = replace(file, relative_path=staged.directory_relative_path + "/../outside.srt")
        else:
            file = replace(file, relative_path="uploads/outside-attempt.srt")
        staged = replace(staged, files=(file, *staged.files[1:]))
    writes = []

    def track(_conn, _cursor, sql, _params, _context, _many):
        if sql.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE", "REPLACE")):
            writes.append(sql)

    event.listen(case.database.engine, "before_cursor_execute", track)
    try:
        with pytest.raises(ProjectCopyError) as error:
            with case.database.immediate_session() as session:
                _publish(session, case, snapshot, staged, creator)
        assert error.value.code == "copy_graph_invalid"
        assert str(error.value) == "The saved project state is incomplete or inconsistent."
        assert not writes
    finally:
        event.remove(case.database.engine, "before_cursor_execute", track)


@pytest.mark.parametrize(
    "copy_id,table,old",
    [
        ("not-a-uuid", "artifacts", "legacy-id"),
        ("00000000-0000-4000-8000-000000000001", "voices", "old"),
        ("00000000-0000-4000-8000-000000000001", "artifacts", ""),
    ],
)
def test_identity_accepts_only_canonical_namespace_and_known_tables(copy_id, table, old):
    with pytest.raises(ProjectCopyError):
        copy_identity(copy_id, table, old)


def test_identity_mapping_is_stable_table_specific_and_never_a_path_operation():
    namespace = "00000000-0000-4000-8000-000000000001"
    old = "legacy/identifier:with punctuation"
    mapped = copy_identity(namespace, "artifacts", old)
    assert mapped == str(uuid.uuid5(uuid.UUID(namespace), "artifacts:" + old))
    assert mapped == copy_identity(namespace, "artifacts", old) and mapped != old
    assert mapped != copy_identity(namespace, "sessions", old)
