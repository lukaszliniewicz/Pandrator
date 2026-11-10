"""Read-only copy capture retains current editable state without copying authority."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import FrozenInstanceError
from pathlib import Path
from typing import Any, cast
from unittest.mock import patch

import pytest
from sqlalchemy import event, text

from pandrator.web import models as m
from pandrator.web.access.contracts import (
    AccessContext,
    AccessDenied,
    ProjectNotFound,
    WorkspaceNotFound,
)
from pandrator.web.accounts.execution_actor import actor_from_principal
from pandrator.web.accounts.models import Workspace, WorkspaceMembership
from pandrator.web.accounts.resource_grant_models import (
    ApiTokenProjectGrant,
    ApiTokenWorkspaceGrant,
)
from pandrator.web.model_base import new_id
from pandrator.web.projects import copy_snapshot as capture_module
from pandrator.web.projects.copy_contracts import ProjectCopyError
from pandrator.web.projects.copy_snapshot import capture_current_project
from pandrator.web.sound_project_models import ProjectSoundPlan
from tests.project_copy_test_support import capture_with_connections, seed_copy_model
from tests.test_project_export_bundles import bundle_case as _bundle_case
from tests.test_project_operation_access import _person, _remove
from tests.test_project_operation_access import team_case as _team_case

bundle_case = _bundle_case
team_case = _team_case


def _context(principal):
    actor = actor_from_principal(principal)
    assert actor is not None
    return AccessContext(actor)


def _capture(case, *, context=None, destination=None, source_project=None, **kwargs):
    with case.database.snapshot_session() as session:
        return capture_with_connections(
            case,
            session,
            context or _context(case.principal),
            source_project or case.source.project_id,
            destination or case.source_workspace,
            settings_service=case.services.workspace_settings,
            **kwargs,
        )


def _file(case, name, *, sid=None, role="upload", state="current", metadata=None):
    path = case.services.paths.uploads / name
    path.write_text("1\n00:00:00,000 --> 00:00:01,000\nStored source.\n", encoding="utf-8")
    artifact = case.services.artifacts.register(
        path,
        kind="srt",
        role=role,
        session_id=sid,
        workspace_id=case.source_workspace,
        metadata=metadata or {},
    )
    if state != "current":
        with case.database.immediate_session() as session:
            session.get(m.Artifact, artifact.id).state = state
    return artifact


def _prepare_copy_case(bundle_case):
    case = bundle_case
    with case.database.snapshot_session() as session:
        case.source_workspace = session.get(m.Project, case.source.project_id).workspace_id
    case.documents, case.revisions, case.segments, case.words = [], [], [], []
    case.old_unselected = []
    for index, branch in enumerate(case.branches):
        with case.database.immediate_session() as session:
            document = m.Document(
                session_id=branch["session_id"],
                stage="translation",
                language=branch["target_language"],
            )
            session.add(document)
            session.flush()
            revisions = [
                m.DocumentRevision(
                    document_id=document.id,
                    revision_number=n,
                    content_hash=f"copy-revision-{index}-{n}",
                    reviewed=n == 2,
                )
                for n in (1, 2, 3)
            ]
            session.add_all(revisions)
            session.flush()
            document.active_revision_id = revisions[1].id
            revisions[1].parent_revision_id = revisions[0].id
            revisions[0].parent_revision_id = revisions[2].id
            segments = [
                m.Segment(
                    revision_id=row.id,
                    ordinal=0,
                    text=f"Saved text {index} {n}",
                    start_ms=100,
                    end_ms=950,
                    speaker="Narrator",
                    metadata_json={"review_note": "Keep this."},
                )
                for n, row in enumerate(revisions)
            ]
            session.add_all(segments)
            session.flush()
            word = m.TimedWord(
                revision_id=revisions[1].id,
                segment_id=segments[1].id,
                ordinal=0,
                text="Saved",
                start_ms=100,
                end_ms=300,
                confidence=0.95,
            )
            session.add(word)
            session.add(
                m.SegmentLineage(parent_segment_id=segments[0].id, child_segment_id=segments[1].id)
            )
            session.add(
                m.SegmentLineage(parent_segment_id=segments[2].id, child_segment_id=segments[0].id)
            )
            session.flush()
            case.documents.append(document.id)
            case.revisions.append([row.id for row in revisions])
            case.segments.append([row.id for row in segments])
            case.words.append(word.id)
        current = _file(
            case,
            f"current-{index}.srt",
            sid=branch["session_id"],
            role="translation",
            metadata={"revision_id": case.revisions[-1][1], "language": branch["target_language"]},
        )
        stale = _file(
            case,
            f"selected-{index}.srt",
            sid=branch["session_id"],
            role="translation",
            state="stale",
            metadata={"revision_id": case.revisions[-1][0]},
        )
        unselected = _file(
            case,
            f"unselected-{index}.srt",
            sid=branch["session_id"],
            role="translation",
            state="stale",
            metadata={"revision_id": case.revisions[-1][2]},
        )
        case.old_unselected.append(unselected.id)
        with case.database.immediate_session() as session:
            # Registering another version advances the default role; restore
            # this fixture's intentional current and separately selected paths.
            session.get(m.Artifact, current.id).state = "current"
            session.merge(
                m.SessionStageSelection(
                    session_id=branch["session_id"], stage_key="translate", artifact_id=stale.id
                )
            )
        if index == 0:
            case.current, case.selected = current, stale
    case.used_library = _file(case, "used-library.srt")
    case.unused_library = _file(case, "unused-library.srt")
    with case.database.immediate_session() as session:
        asset = m.SourceAsset(
            workspace_id=case.source_workspace,
            artifact_id=case.used_library.id,
            display_name="Used editable source",
            kind="srt",
            content_hash=case.used_library.content_hash,
        )
        unused = m.SourceAsset(
            workspace_id=case.source_workspace,
            artifact_id=case.unused_library.id,
            display_name="Unused library source",
            kind="srt",
            content_hash=case.unused_library.content_hash,
        )
        session.add_all([asset, unused])
        session.flush()
        session.add(
            m.SessionSource(session_id=case.source.id, source_asset_id=asset.id, role="reference")
        )
        session.add(
            m.SourceRecord(
                session_id=case.source.id,
                kind="srt",
                display_name="Legacy used source",
                artifact_id=case.used_library.id,
            )
        )
        session.add(
            m.ArtifactEdge(
                parent_artifact_id=case.used_library.id, child_artifact_id=case.current.id
            )
        )
        case.used_asset, case.unused_asset = asset.id, unused.id
    return case


@pytest.fixture
def unbound_copy_case(bundle_case):
    return _prepare_copy_case(bundle_case)


@pytest.fixture
def copy_case(bundle_case):
    case = _prepare_copy_case(bundle_case)
    seed_copy_model(case, case.source_workspace)
    return case


def test_two_language_current_graph_is_complete_bounded_and_immutable(copy_case):
    case = copy_case
    snapshot = _capture(case)
    assert snapshot.ready, snapshot.blockers
    state = json.loads(snapshot.state_json)["rows"]
    assert len(state["sessions"]) == 3
    assert len(state["translation_project_branches"]) == 2
    selected = {row["id"] for row in state["artifacts"]}
    assert {case.current.id, case.selected.id, case.used_library.id, case.checkpoint.id} <= selected
    assert not selected.intersection([case.unused_library.id, *case.old_unselected])
    assert {branch["source_checkpoint_artifact_id"] for branch in case.branches} <= selected
    assert {row["id"] for row in state["source_assets"]} == {case.used_asset}
    assert case.unused_asset not in snapshot.state_json
    revision_ids = {row["id"] for row in state["document_revisions"]}
    assert {value for revisions in case.revisions for value in revisions[:2]} <= revision_ids
    assert not revision_ids.intersection(revisions[2] for revisions in case.revisions)
    assert len(state["segment_lineage"]) == 2
    assert any(
        row["text"].startswith("Saved text") and row["start_ms"] == 100 for row in state["segments"]
    )
    assert any(row["reviewed"] for row in state["document_revisions"])
    assert {row["id"] for row in state["timed_words"]} == set(case.words)
    assert any(
        row["parent_artifact_id"] == case.used_library.id
        and row["child_artifact_id"] == case.current.id
        for row in state["artifact_edges"]
    )
    assert snapshot.fingerprint == hashlib.sha256(snapshot.state_json.encode()).hexdigest()
    with pytest.raises(FrozenInstanceError):
        cast(Any, snapshot.files[0]).relative_path = "changed"
    state["segments"][0]["text"] = "Changed decoded copy"
    assert "Changed decoded copy" not in snapshot.state_json
    manifest = snapshot.manifest()
    public = json.dumps(manifest)
    assert manifest["known_bytes"] > 0 and manifest["unknown_size_count"] == 0
    for marker in [
        "relative_path",
        "state_json",
        "Saved text",
        "review_note",
        "uploads/",
        "value_json",
        "api_key",
        "prompt",
    ]:
        assert marker not in public


def test_viewing_grants_and_operational_activity_do_not_change_content_fingerprint(copy_case):
    case = copy_case
    before = _capture(case)
    assert _capture(case).fingerprint == before.fingerprint
    viewer = _person(case)
    with case.database.immediate_session() as session:
        session.add(
            m.ProjectMembership(
                project_id=case.source.project_id, user_id=viewer.user_id, role="viewer"
            )
        )
        root = session.get(m.Project, case.source.project_id)
        root.revision += 1
        root.updated_at = m.utcnow()
        record = session.get(m.SessionRecord, case.source.id)
        record.status, record.revision = "running", record.revision + 1
        session.add(
            m.Job(
                kind="copy-test-active",
                project_id=root.id,
                workspace_id=root.workspace_id,
                session_id=case.source.id,
                status="queued",
                payload_json={"secret": "never in snapshot"},
            )
        )
        session.add(
            m.AgentRun(
                session_id=case.source.id,
                status="completed",
                settings_json={"history_secret": "never"},
            )
        )
    after = _capture(case)
    assert after.fingerprint == before.fingerprint
    assert after.active_job_count == before.active_job_count + 1
    assert after.manifest()["active_job_count"] == after.active_job_count
    assert "copy-test-active" not in after.state_json and "history_secret" not in after.state_json


@pytest.mark.parametrize("change", ["text", "selection", "settings", "language"])
def test_saved_edit_changes_fingerprint(copy_case, change):
    case = copy_case
    before = _capture(case)
    with case.database.immediate_session() as session:
        if change == "text":
            session.get(m.Segment, case.segments[0][1]).text = "A saved correction."
        elif change == "selection":
            session.get(
                m.SessionStageSelection, (case.branches[0]["session_id"], "translate")
            ).artifact_id = case.current.id
        elif change == "settings":
            session.merge(
                m.SessionSetting(
                    session_id=case.source.id,
                    section="subtitles",
                    value_json={"max_chars_per_line": 31},
                )
            )
        else:
            session.get(m.SessionRecord, case.branches[0]["session_id"]).target_language = "fr"
    assert _capture(case).fingerprint != before.fingerprint


def test_effective_private_defaults_are_sanitized_and_connections_flagged(copy_case):
    case = copy_case
    sentinel = "literal-source-credential-never-copy"
    endpoint = "https://private-source-endpoint.invalid/v1"
    with case.database.immediate_session() as session:
        session.merge(
            m.WorkspaceSetting(
                workspace_id=case.source_workspace,
                key="defaults.subtitles",
                value_json={"max_chars_per_line": 33, "max_cps": 13.5},
            )
        )
        session.merge(
            m.SessionSetting(
                session_id=case.source.id,
                section="translation",
                value_json={
                    "api_key": sentinel,
                    "base_url": endpoint,
                    "credential_ref": "source-credential-id",
                    "provider_id": "source-provider-id",
                    "nested": {"access_token": sentinel, "llm_endpoint": endpoint},
                    "prompt": "Retain the citation URL https://reference.invalid/book.",
                },
            )
        )
        session.merge(
            m.SessionSetting(
                session_id=case.source.id,
                section="tts",
                value_json={
                    "service_configs": {"legacy": {"base_url": endpoint, "api_key": sentinel}}
                },
            )
        )
        session.merge(
            m.SessionSetting(
                session_id=case.source.id,
                section="multilingual_setup",
                value_json={"languages": ["ja", "pl"]},
            )
        )
    snapshot = _capture(case)
    assert ("destination_binding_required", "translation") in snapshot.blockers
    assert ("destination_binding_required", "tts") in snapshot.blockers
    assert "https://reference.invalid/book." in snapshot.state_json
    for secret in [sentinel, endpoint, "source-credential-id", "source-provider-id"]:
        assert secret not in snapshot.state_json and secret not in json.dumps(snapshot.manifest())
    state = json.loads(snapshot.state_json)
    settings = state["rows"]["session_settings"]
    assert all(
        row["value_json"]["max_chars_per_line"] == 33
        for row in settings
        if row["section"] == "subtitles"
    )
    assert any(row["section"] == "multilingual_setup" for row in settings)
    assert state["binding_fields"][f"{case.source.id}:translation"]["secret"] == 2


def test_capture_performs_no_writes_or_file_probes_and_uses_bulk_artifact_policy(copy_case):
    case = copy_case
    statements = []

    def record(_conn, _cursor, statement, _params, _context, _many):
        statements.append(statement)

    event.listen(case.database.engine, "before_cursor_execute", record)
    try:
        with case.database.snapshot_session() as session:
            with (
                patch.object(session, "flush", side_effect=AssertionError("No flush")),
                patch.object(session, "commit", side_effect=AssertionError("No commit")),
                patch.object(Path, "stat", side_effect=AssertionError("No file stat")),
                patch.object(Path, "open", side_effect=AssertionError("No file read")),
                patch("builtins.open", side_effect=AssertionError("No file open")),
                patch.object(
                    capture_module,
                    "visible_artifact_ids_statement",
                    wraps=capture_module.visible_artifact_ids_statement,
                ) as scoped,
            ):
                snapshot = capture_current_project(
                    session,
                    _context(case.principal),
                    case.source.project_id,
                    case.source_workspace,
                    settings_service=case.services.workspace_settings,
                )
                assert scoped.call_count <= len(snapshot.files)
                assert scoped.call_count < 5
    finally:
        event.remove(case.database.engine, "before_cursor_execute", record)
    assert not any(
        sql.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE", "REPLACE"))
        for sql in statements
    )
    assert not any("PRAGMA" not in sql and "stored_credentials" in sql for sql in statements)


@pytest.fixture
def auth_case(team_case):
    case = team_case
    case.source_workspace = case.workspace_id
    return case


@pytest.mark.parametrize(
    "denial",
    ["foreign_admin", "reader", "removed", "narrowed", "source_scope", "destination_scope"],
)
def test_source_edit_and_destination_creation_are_both_current_authority(auth_case, denial):
    case = auth_case
    principal = case.principal
    if denial in {"foreign_admin", "reader"}:
        principal = _person(case, workspace_id=case.workspace_id, admin=denial == "foreign_admin")
        if denial == "reader":
            with case.database.immediate_session() as session:
                session.add(
                    m.ProjectMembership(
                        project_id=case.source.project_id, user_id=principal.user_id, role="viewer"
                    )
                )
    elif denial == "removed":
        _remove(case)
    elif denial == "narrowed":
        with case.database.immediate_session() as session:
            session.get(m.ApiToken, principal.token_id).scopes_json = ["app.read"]
    elif denial == "source_scope":
        with case.database.immediate_session() as session:
            session.delete(
                session.get(ApiTokenWorkspaceGrant, (principal.token_id, case.workspace_id))
            )
    else:
        destination = Workspace(id=new_id(), kind="team", name="Unrelated destination")
        with case.database.immediate_session() as session:
            session.add(destination)
        with pytest.raises(WorkspaceNotFound):
            _capture(case, destination=destination.id)
        return
    with pytest.raises((AccessDenied, ProjectNotFound, WorkspaceNotFound)):
        _capture(case, context=_context(principal))


def test_project_scoped_credential_cannot_copy_rootless_library_dependency(auth_case):
    case = auth_case
    library = _file(case, "library-dependency.srt")
    with case.database.immediate_session() as session:
        session.add(
            m.ArtifactEdge(parent_artifact_id=library.id, child_artifact_id=case.checkpoint.id)
        )
        destination = Workspace(id=new_id(), kind="team", name="Allowed copy destination")
        session.add(destination)
        session.flush()
        session.add(
            WorkspaceMembership(
                workspace_id=destination.id, user_id=case.principal.user_id, role="member"
            )
        )
        session.delete(
            session.get(ApiTokenWorkspaceGrant, (case.principal.token_id, case.workspace_id))
        )
        session.add(
            ApiTokenProjectGrant(
                token_id=case.principal.token_id, project_id=case.source.project_id
            )
        )
        session.add(
            ApiTokenWorkspaceGrant(token_id=case.principal.token_id, workspace_id=destination.id)
        )
    with pytest.raises(AccessDenied):
        _capture(case, destination=destination.id)


@pytest.mark.parametrize("state", ["source_trash", "source_missing", "destination_archived"])
def test_unavailable_source_or_destination_is_rejected(copy_case, state):
    case = copy_case
    destination = None
    source_project = None
    with case.database.immediate_session() as session:
        if state == "source_trash":
            session.get(m.SessionRecord, case.source.id).trashed_at = m.utcnow()
        elif state == "source_missing":
            # The graph trigger correctly forbids clearing an attached source.
            # An empty retained root is the valid no-source state to inspect.
            source_project = new_id()
            session.add(
                m.Project(
                    id=source_project,
                    workspace_id=case.source_workspace,
                    name="Source no longer retained",
                    created_by_user_id=case.principal.user_id,
                )
            )
        else:
            destination = new_id()
            session.add(
                Workspace(id=destination, kind="team", name="Archived target", state="archived")
            )
            session.flush()
            session.add(
                WorkspaceMembership(
                    workspace_id=destination, user_id=case.principal.user_id, role="owner"
                )
            )
    if state == "destination_archived":
        with pytest.raises(WorkspaceNotFound):
            _capture(case, destination=destination)
    else:
        with pytest.raises(ProjectCopyError) as error:
            _capture(case, source_project=source_project)
        assert error.value.code == "copy_source_unavailable"


def test_unknown_session_table_and_pending_editorial_are_explicit_blockers(copy_case):
    case = copy_case
    with case.database.immediate_session() as session:
        session.execute(
            text(
                "CREATE TABLE future_copy_state (id TEXT PRIMARY KEY, owner_session TEXT REFERENCES sessions(id), secret_body TEXT)"
            )
        )
        session.execute(
            text(
                "INSERT INTO future_copy_state VALUES ('future', :sid, 'never copy this protected body')"
            ),
            {"sid": case.source.id},
        )
        session.add(
            m.AgentRun(
                session_id=case.source.id,
                status="ready",
                settings_json={"secret_body": "never copy this editorial body"},
            )
        )
        session.add(
            m.SessionSetting(
                session_id=case.source.id,
                section="custom_plugin",
                value_json={"private": "never silently copy"},
            )
        )
    snapshot = _capture(case)
    assert {
        ("unsupported_state", "future_copy_state"),
        ("unsupported_state", "editorial_operation"),
        ("unsupported_state", "custom_settings"),
    } <= set(snapshot.blockers)
    assert (
        "never copy this" not in snapshot.state_json
        and "never silently copy" not in snapshot.state_json
    )


def test_saved_unsupported_domains_are_counted_without_their_bodies(copy_case):
    case = copy_case
    sid = case.branches[0]["session_id"]
    with case.database.immediate_session() as session:
        generation = m.GenerationPlan(session_id=sid)
        session.add(generation)
        session.flush()
        revision = m.GenerationPlanRevision(
            plan_id=generation.id,
            revision_number=1,
            content_hash="unsupported",
            settings_json={"body": "never-copy-generation"},
        )
        session.add(revision)
        session.flush()
        session.add_all(
            [
                ProjectSoundPlan(session_id=sid, revision=1),
                m.PerformancePlan(
                    session_id=sid,
                    plan_revision_id=revision.id,
                    base_signature="saved",
                    units_json=[{"body": "never-copy-performance"}],
                ),
                m.PronunciationEntry(
                    workspace_id=case.source_workspace,
                    project_id=case.source.project_id,
                    scope="session",
                    session_id=sid,
                    source_form="never-copy-pronunciation",
                    normalized_form="never-copy-pronunciation",
                    phonetic="saved",
                ),
                m.SubtitleEvidence(
                    session_id=sid,
                    source_artifact_id=case.current.id,
                    source_revision_id=case.revisions[0][1],
                    cue_id=0,
                    start_ms=100,
                    end_ms=950,
                    clip_start_ms=0,
                    clip_end_ms=1000,
                    reason="never-copy-evidence",
                ),
                m.KnowledgeLedger(
                    session_id=sid, kind="terms", payload_json={"body": "never-copy-ledger"}
                ),
                m.OutputAssembly(
                    session_id=sid, status="ready", settings_json={"body": "never-copy-output"}
                ),
            ]
        )
    snapshot = _capture(case)
    assert {("unsupported_state", domain) for domain in capture_module._UNSUPPORTED} <= set(
        snapshot.blockers
    )
    assert "never-copy-" not in snapshot.state_json
    assert json.loads(snapshot.state_json)["rows"]["subtitle_evidence"] == []
    assert all(
        count == 1 for count in json.loads(snapshot.state_json)["unsupported_counts"].values()
    )


@pytest.mark.parametrize(
    "kind",
    [
        "external_source",
        "unhashed_file",
        "reusable_editorial_dependency",
        "trashed_language_versions",
        "workflow_kind",
    ],
)
def test_unsupported_dependencies_and_lifecycle_never_look_ready(copy_case, kind):
    case = copy_case
    with case.database.immediate_session() as session:
        if kind == "external_source":
            session.add(
                m.SourceRecord(
                    session_id=case.source.id,
                    kind="external",
                    display_name="External input",
                    external_path="/outside/managed/root",
                )
            )
        elif kind == "unhashed_file":
            session.get(m.Artifact, case.used_library.id).content_hash = None
        elif kind == "reusable_editorial_dependency":
            session.add(
                m.SessionSetting(
                    session_id=case.source.id,
                    section="tts",
                    value_json={"voice_ref": {"kind": "managed", "voice_id": new_id()}},
                )
            )
        elif kind == "trashed_language_versions":
            session.get(m.SessionRecord, case.branches[1]["session_id"]).trashed_at = m.utcnow()
        else:
            # Copy supports subtitles, media_edit, and voiceover. Audiobook
            # remains outside the current copy contract and must not look ready.
            session.get(m.SessionRecord, case.branches[1]["session_id"]).workflow_kind = "audiobook"
    snapshot = _capture(case)
    assert ("unsupported_state", kind) in snapshot.blockers
    assert not snapshot.ready


@pytest.mark.parametrize("limit", ["sessions", "artifacts", "editorial"])
def test_capture_limit_raises_instead_of_truncating(copy_case, monkeypatch, limit):
    case = copy_case
    assert (
        capture_module.MAX_SESSIONS,
        capture_module.MAX_ARTIFACTS,
        capture_module.MAX_EDITORIAL_ROWS,
    ) == (101, 10_000, 250_000)
    name = {
        "sessions": "MAX_SESSIONS",
        "artifacts": "MAX_ARTIFACTS",
        "editorial": "MAX_EDITORIAL_ROWS",
    }[limit]
    monkeypatch.setattr(capture_module, name, 1)
    with pytest.raises(ProjectCopyError) as error:
        _capture(case)
    assert error.value.code == "copy_limit_exceeded"


@pytest.mark.parametrize("pointer", ["missing_revision", "deleted_selection"])
def test_required_missing_or_deleted_pointers_are_graph_invalid(copy_case, pointer):
    case = copy_case
    with case.database.immediate_session() as session:
        if pointer == "missing_revision":
            document = session.get(m.Document, case.documents[0])
            document.active_revision_id = new_id()
        else:
            session.get(m.Artifact, case.selected.id).state = "deleted"
    with pytest.raises(ProjectCopyError) as error:
        _capture(case)
    assert error.value.code == "copy_graph_invalid"


def test_name_and_required_context_are_explicit(copy_case):
    case = copy_case
    assert _capture(case).name == case.project["name"]
    assert _capture(case, name="  Independent copy  ").name == "Independent copy"
    for value in ["", "   ", "x" * 256]:
        with pytest.raises(ValueError):
            _capture(case, name=value)
    with case.database.snapshot_session() as session:
        with pytest.raises(TypeError):
            capture_current_project(
                session,
                cast(Any, None),
                case.source.project_id,
                case.source_workspace,
                settings_service=case.services.workspace_settings,
            )
        with pytest.raises(TypeError):
            cast(Any, capture_current_project)(
                session, _context(case.principal), case.source.project_id, case.source_workspace
            )


def test_artifact_scope_and_in_predicates_are_chunked_at_500(copy_case):
    case = copy_case
    with case.database.immediate_session() as session:
        session.add_all(
            m.Artifact(
                workspace_id=case.source_workspace,
                project_id=case.source.project_id,
                session_id=case.source.id,
                kind="srt",
                role="saved_attachment",
                relative_path=f"uploads/bounded-copy-{index}.srt",
                content_hash="1" * 64,
                size_bytes=1,
            )
            for index in range(520)
        )
    statements = []

    def record(_conn, _cursor, statement, _params, _context, _many):
        statements.append(statement)

    event.listen(case.database.engine, "before_cursor_execute", record)
    try:
        with patch.object(
            capture_module,
            "visible_artifact_ids_statement",
            wraps=capture_module.visible_artifact_ids_statement,
        ) as scoped:
            snapshot = _capture(case)
            # Capture authorizes the artifact closure once per convergence pass;
            # this fixture needs two passes, and each pass must chunk at 500.
            assert scoped.call_count == 2 * -(-len(snapshot.files) // 500)
    finally:
        event.remove(case.database.engine, "before_cursor_execute", record)
    assert len(snapshot.files) >= 520
    groups = [group for sql in statements for group in re.findall(r"\bIN\s*\(([^()]*)\)", sql)]
    assert all(group.count("?") <= 500 for group in groups)
    assert any(group.count("?") == 500 for group in groups)


@pytest.mark.parametrize(
    "setting",
    [
        {"voice": "managed:existing-voice"},
        {"voice": "00000000-0000-4000-8000-000000000001"},
        {"source_artifact_id": "outside-base-graph"},
        {"sound_variant_id": "outside-base-graph"},
    ],
)
def test_saved_reusable_dependency_ids_block_without_being_silently_removed(copy_case, setting):
    case = copy_case
    with case.database.immediate_session() as session:
        session.merge(
            m.SessionSetting(session_id=case.source.id, section="tts", value_json=setting)
        )
    snapshot = _capture(case)
    assert ("unsupported_state", "reusable_editorial_dependency") in snapshot.blockers
    settings = json.loads(snapshot.state_json)["rows"]["session_settings"]
    copied = next(
        row["value_json"]
        for row in settings
        if row["session_id"] == case.source.id and row["section"] == "tts"
    )
    assert setting.items() <= copied.items()
