"""Source status/change boundaries with disposable users, roots and reset files."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, Mock

import pytest
from sqlalchemy import event, func, select

from pandrator.runtime import DataPaths
from pandrator.web import source_management as source
from pandrator.web.access import source_management_policy as policy
from pandrator.web.access.contracts import AccessDenied, WorkspaceNotFound
from pandrator.web.access.session_policy import SessionNotFound
from pandrator.web.accounts.execution_actor import execution_actor_scope
from pandrator.web.accounts.models import WorkspaceMembership
from pandrator.web.models import (
    ApiToken,
    Artifact,
    OutcomePlan,
    SessionRecord,
    SessionSetting,
    SourceAsset,
    new_id,
    utcnow,
)
from pandrator.web.projects.models import Project, ProjectMembership
from pandrator.web.sessions import SessionService
from pandrator.web.source_library import SourceLibraryService
from tests.test_job_access_policy import _context
from tests.test_job_access_policy import block_network_attempts as block_network_attempts
from tests.test_job_access_policy import case as case

READ = frozenset({"app.read"})
EDIT = frozenset({"app.read", "app.write"})


@pytest.fixture
def management(case):
    with case.database.session() as unit:
        unit.add(WorkspaceMembership(workspace_id=case.team, user_id=case.user, role="member"))
        unit.add(ProjectMembership(project_id=case.a, user_id=case.other, role="viewer"))
        target = unit.scalar(select(Project.source_session_id).where(Project.id == case.a))
        foreign = SourceAsset(
            workspace_id=case.personal[case.outsider],
            display_name="Foreign source",
            kind="text",
            metadata_json={"private": True},
        )
        unit.add(foreign)
        unit.flush()
    services = SimpleNamespace(
        database=case.database,
        paths=DataPaths(case.database.path.parent),
        sessions=SessionService(case.database),
        source_library=SourceLibraryService(case.database),
    )
    return SimpleNamespace(case=case, services=services, target=target, foreign=foreign.id)


def _member(m, *, scopes=EDIT):
    return _context(m.case, m.case.user, scopes=scopes, workspaces=(m.case.team,))


def test_none_leaf_and_public_projection_keep_old_query_shape(management):
    m = management
    unit: Any = MagicMock()
    with execution_actor_scope(None):
        policy.require_source_change_access(unit, "trusted", source_asset_id="trusted-source")
        assert policy.require_source_session_creation_access(unit, "trusted") is None
    assert unit.mock_calls == []
    queries = []

    def capture(_connection, _cursor, statement, _parameters, _context, _executemany):
        if statement.lstrip().upper().startswith("SELECT"):
            queries.append(statement)

    event.listen(m.case.database.engine, "before_cursor_execute", capture)
    try:
        with execution_actor_scope(None), m.case.database.session() as reader:
            public = source.source_status(reader, m.target)
        first = len(queries)
        queries.clear()
        with execution_actor_scope(None), m.case.database.session() as reader:
            private = source._source_status(reader, m.target)
        assert public == private and first == len(queries)
    finally:
        event.remove(m.case.database.engine, "before_cursor_execute", capture)


@pytest.mark.parametrize("reader", ["member_read_only", "private_viewer", "foreign_admin"])
def test_read_boundary_and_denied_mutations_precede_private_projection_and_native(
    management, monkeypatch, reader
):
    m = management
    if reader == "member_read_only":
        context = _member(m, scopes=READ)
    elif reader == "private_viewer":
        context = _context(m.case, m.case.other, scopes=EDIT, projects=(m.case.a,))
    else:
        context = _context(
            m.case,
            m.case.outsider,
            scopes=EDIT | {"app.admin"},
            workspaces=(m.case.personal[m.case.outsider],),
        )
    with execution_actor_scope(context.actor), m.case.database.session() as unit:
        if reader == "foreign_admin":
            with pytest.raises(SessionNotFound):
                source.source_status(unit, m.target)
        else:
            assert source.source_status(unit, m.target)["session_id"] == m.target
    projection, impact, native = Mock(), Mock(), Mock()
    monkeypatch.setattr(source, "_source_status", projection)
    monkeypatch.setattr(source, "_impact", impact)
    monkeypatch.setattr(source, "_validate_source", native)
    with execution_actor_scope(context.actor), m.case.database.immediate_session() as unit:
        commands = (
            lambda: source.preview_source_change(unit, m.target, "primary", None),
            lambda: source.change_source_in_session(
                m.services,
                unit,
                m.target,
                role="primary",
                new_source_asset_id=None,
                expected_revision=1,
                impact_token="unread",
            ),
            lambda: source.start_new_source_session_in_session(
                m.services,
                unit,
                m.target,
                role="primary",
                new_source_asset_id=None,
                expected_revision=1,
                impact_token="unread",
            ),
            lambda: source.confirm_recording_timing(
                unit, m.target, expected_revision=1, media_artifact_id="unread"
            ),
        )
        for command in commands:
            with pytest.raises((AccessDenied, SessionNotFound)):
                command()
    projection.assert_not_called()
    impact.assert_not_called()
    native.assert_not_called()


def test_selected_foreign_source_denies_preview_and_change_before_body_or_hash(
    management, monkeypatch
):
    m = management
    projection, impact, native = Mock(), Mock(), Mock()
    monkeypatch.setattr(source, "_source_status", projection)
    monkeypatch.setattr(source, "_impact", impact)
    monkeypatch.setattr(source, "_validate_source", native)
    with execution_actor_scope(_member(m).actor), m.case.database.immediate_session() as unit:
        with pytest.raises(KeyError):
            source.preview_source_change(unit, m.target, "primary", m.foreign)
        with pytest.raises(KeyError):
            source.change_source_in_session(
                m.services,
                unit,
                m.target,
                role="primary",
                new_source_asset_id=m.foreign,
                expected_revision=1,
                impact_token="unread",
            )
    projection.assert_not_called()
    impact.assert_not_called()
    native.assert_not_called()


def test_member_change_after_creator_departure_reuses_authorized_projection(
    management, monkeypatch
):
    m = management
    # Construct the creator attribution with the new root; do not mutate a root.
    with m.case.database.session() as unit:
        project = Project(
            workspace_id=m.case.team,
            source_session_id=None,
            name="Creator's original",
            created_by_user_id=m.case.owner,
        )
        unit.add(project)
        unit.flush()
        original = SessionRecord(name="Creator's original", project_id=project.id)
        unit.add(original)
        unit.flush()
        project.source_session_id = original.id
        unit.flush()
        owner = unit.get(WorkspaceMembership, (m.case.team, m.case.owner))
        assert owner is not None
        unit.delete(owner)
        target, project_id = original.id, project.id
    guard = Mock(wraps=source.require_source_change_access)
    public_projection = Mock(
        side_effect=AssertionError("Already-proved units use the private projection")
    )
    monkeypatch.setattr(source, "require_source_change_access", guard)
    monkeypatch.setattr(source, "source_status", public_projection)
    with execution_actor_scope(_member(m).actor), m.case.database.immediate_session() as unit:
        preview = source.preview_source_change(unit, target, "primary", None)
        result = source.change_source_in_session(
            m.services,
            unit,
            target,
            role="primary",
            new_source_asset_id=None,
            expected_revision=preview["session_revision"],
            impact_token=preview["impact_token"],
        )
        assert (
            result["unchanged"] is False
            and result["session_revision"] == preview["session_revision"] + 1
        )
        assert unit.get(Project, project_id).created_by_user_id == m.case.owner
    assert guard.call_count == 2
    public_projection.assert_not_called()


@pytest.mark.parametrize("revoked", ["token", "role"])
def test_current_writer_token_or_role_loss_blocks_before_impact(management, monkeypatch, revoked):
    m = management
    context = _member(m)
    with m.case.database.session() as unit:
        if revoked == "token":
            token = unit.get(ApiToken, context.actor.token_id)
            assert token is not None
            token.revoked_at = utcnow()
        else:
            member = unit.get(WorkspaceMembership, (m.case.team, m.case.user))
            assert member is not None
            unit.delete(member)
    impact = Mock()
    monkeypatch.setattr(source, "_impact", impact)
    with execution_actor_scope(context.actor), m.case.database.immediate_session() as unit:
        with pytest.raises((AccessDenied, SessionNotFound)):
            source.preview_source_change(unit, m.target, "primary", None)
    impact.assert_not_called()


def test_recording_confirmation_uses_edit_entry_and_private_projection(management, monkeypatch):
    m = management
    with m.case.database.session() as unit:
        unit.add(
            SessionSetting(
                session_id=m.target,
                section="_recording_timing_review",
                revision=1,
                value_json={"required": True, "media_artifact_id": "media"},
            )
        )
    guard = Mock(wraps=source.require_source_change_access)
    monkeypatch.setattr(source, "require_source_change_access", guard)
    monkeypatch.setattr(
        source,
        "_source_status",
        Mock(return_value={"session_revision": 1, "media": {"artifact_id": "media"}}),
    )
    monkeypatch.setattr(
        source, "source_status", Mock(side_effect=AssertionError("No redundant public READ proof"))
    )
    with execution_actor_scope(_member(m).actor), m.case.database.immediate_session() as unit:
        result = source.confirm_recording_timing(
            unit, m.target, expected_revision=1, media_artifact_id="media"
        )
        timing = unit.get(SessionSetting, (m.target, "_recording_timing_review"))
        assert (
            timing is not None and timing.value_json["required"] is False and timing.revision == 2
        )
        assert result["timing_verified"] is True
    guard.assert_called_once()


def test_committed_tombstone_cleanup_survives_role_loss_and_preserves_inputs(
    management, monkeypatch
):
    m = management
    context = _member(m)
    paths = {}
    with m.case.database.session() as unit:
        for label, state in (("tombstone", "deleted"), ("library", "deleted"), ("live", "current")):
            relative = f"artifacts/{new_id()}.txt"
            path = m.services.paths.managed_path(relative)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(label, encoding="utf-8")
            paths[label] = path
            artifact = Artifact(
                workspace_id=m.case.team,
                project_id=m.case.a,
                session_id=m.target,
                kind="text",
                role="derived",
                state=state,
                relative_path=relative,
                metadata_json={"source_reset_cleanup_pending": True},
            )
            unit.add(artifact)
            unit.flush()
            if label == "library":
                unit.add(
                    SourceAsset(
                        workspace_id=m.case.team,
                        artifact_id=artifact.id,
                        display_name="Protected original",
                        kind="text",
                    )
                )
        member = unit.get(WorkspaceMembership, (m.case.team, m.case.user))
        assert member is not None
        unit.delete(member)
    gate = Mock(side_effect=AssertionError("Committed cleanup must not acquire actor authority"))
    monkeypatch.setattr(source, "require_source_change_access", gate)
    with execution_actor_scope(context.actor):
        result = source.cleanup_reset_files(m.services, m.target)
    assert result == {"files_removed": 1, "cleanup_pending": 1}
    assert not paths["tombstone"].exists() and paths["library"].exists() and paths["live"].exists()
    gate.assert_not_called()


def test_start_new_member_preserves_original_and_uses_actual_caller_attribution(management):
    m = management
    with m.case.database.session() as unit:
        original = unit.get(SessionRecord, m.target)
        assert original is not None
        before = (original.name, original.revision, original.project_id)
        unit.add(
            SessionSetting(
                session_id=m.target, section="tts", value_json={"language": "en"}, revision=4
            )
        )
        unit.add(
            SessionSetting(
                session_id=m.target,
                section="_recording_timing_review",
                value_json={"required": True},
            )
        )
        unit.add(OutcomePlan(session_id=m.target, value_json={"intent": "keep"}, revision=5))
    context = _member(m)
    with execution_actor_scope(context.actor), m.case.database.immediate_session() as unit:
        preview = source.preview_source_change(unit, m.target, "primary", None)
        result = source.start_new_source_session_in_session(
            m.services,
            unit,
            m.target,
            role="primary",
            new_source_asset_id=None,
            expected_revision=preview["session_revision"],
            impact_token=preview["impact_token"],
        )
        created = unit.get(SessionRecord, result["session_id"])
        original = unit.get(SessionRecord, m.target)
        assert created is not None and original is not None
        project = unit.get(Project, created.project_id)
        assert project is not None and project.workspace_id == m.case.team
        assert project.created_by_user_id == m.case.user
        assert (original.name, original.revision, original.project_id) == before
        assert result["original_preserved"] is True and result["original_session_id"] == m.target
        copied_setting = unit.get(SessionSetting, (created.id, "tts"))
        source_setting = unit.get(SessionSetting, (m.target, "tts"))
        copied_outcome = unit.get(OutcomePlan, created.id)
        original_outcome = unit.get(OutcomePlan, m.target)
        assert copied_setting is not None and source_setting is not None
        assert copied_setting.value_json == source_setting.value_json == {"language": "en"}
        assert (
            copied_setting.value_json is not source_setting.value_json
            and copied_setting.revision == 1
        )
        assert copied_outcome is not None and original_outcome is not None
        assert copied_outcome.value_json == original_outcome.value_json == {"intent": "keep"}
        assert (
            copied_outcome.value_json is not original_outcome.value_json
            and copied_outcome.revision == 1
        )
        assert unit.get(SessionSetting, (created.id, "_recording_timing_review")) is None
        assert (
            list(
                unit.scalars(
                    select(ProjectMembership).where(ProjectMembership.project_id == project.id)
                )
            )
            == []
        )


def test_start_new_project_only_edit_credential_denies_before_impact_native_or_creation(
    management, monkeypatch
):
    m = management
    context = _context(m.case, m.case.user, scopes=EDIT, projects=(m.case.a,))
    impact, native, projection = Mock(), Mock(), Mock()
    create = Mock(wraps=m.services.sessions.create)
    monkeypatch.setattr(source, "_impact", impact)
    monkeypatch.setattr(source, "_validate_source", native)
    monkeypatch.setattr(source, "_source_status", projection)
    monkeypatch.setattr(m.services.sessions, "create", create)
    with execution_actor_scope(context.actor), m.case.database.immediate_session() as unit:
        policy.require_source_change_access(unit, m.target)
        count_before = unit.scalar(select(func.count()).select_from(SessionRecord))
        with pytest.raises(WorkspaceNotFound):
            source.start_new_source_session_in_session(
                m.services,
                unit,
                m.target,
                role="primary",
                new_source_asset_id=None,
                expected_revision=1,
                impact_token="not-read",
            )
        assert unit.scalar(select(func.count()).select_from(SessionRecord)) == count_before
    impact.assert_not_called()
    native.assert_not_called()
    projection.assert_not_called()
    create.assert_not_called()
