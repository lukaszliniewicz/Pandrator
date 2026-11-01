from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import event

from pandrator.web.accounts.identity import initial_personal_workspace
from pandrator.web.accounts.models import Workspace
from pandrator.web.database import Database
from pandrator.web.models import (
    OutcomePlan,
    SessionRecord,
    SessionSetting,
    WorkspaceSetting,
)
from pandrator.web.sessions import SessionService
from pandrator.web.settings_policy import BUILTIN_DEFAULTS, stable_hash
from pandrator.web.workspace_settings import WorkspaceSettingsService
from tests.test_settings_workspace_scope import (
    SettingsCase,
    seed_workspace_setting,
)
from tests.web_test_support import prepare_web_test_data_root


@pytest.fixture
def settings_case(tmp_path: Path) -> Iterator[SettingsCase]:
    paths = prepare_web_test_data_root(tmp_path)
    database = Database(paths.database)
    workspaces: dict[str, str] = {}
    with database.immediate_session() as session:
        initial_personal_workspace(session)
        for name in ("alpha", "beta"):
            workspace = Workspace(kind="team", name=name)
            session.add(workspace)
            session.flush()
            workspaces[name] = workspace.id
    sessions = {
        name: SessionService(database).create(name, workspace_id=workspaces[name]).id
        for name in ("alpha", "beta")
    }
    try:
        yield SettingsCase(database, paths, workspaces, sessions)
    finally:
        database.dispose()


def _read_subtitle_composition(case: SettingsCase, session_id: str):
    service = WorkspaceSettingsService(case.database)
    snapshot = service.get(session_id, "subtitles")
    resolved, digest = service.resolve(session_id, sections=["subtitles"])
    return snapshot, resolved, digest


def test_subtitle_settings_snapshot_batches_only_its_composition_reads(
    settings_case: SettingsCase,
) -> None:
    case = settings_case
    session_id = case.sessions["alpha"]
    workspace_id = case.workspaces["alpha"]
    other_workspace_id = case.workspaces["beta"]
    service = WorkspaceSettingsService(case.database)

    seed_workspace_setting(
        case.database,
        workspace_id,
        "defaults.subtitles",
        {"max_chars_per_line": 42, "max_cps": 19, "language_defaults": True},
        revision=2,
    )
    seed_workspace_setting(
        case.database,
        workspace_id,
        "defaults.stt",
        {"subtitle_max_chars_per_line": 59, "subtitle_max_lines": 2},
        revision=3,
    )
    seed_workspace_setting(
        case.database,
        workspace_id,
        "defaults.translation",
        {"target_language": "de"},
        revision=4,
    )
    seed_workspace_setting(
        case.database,
        other_workspace_id,
        "defaults.subtitles",
        {"max_chars_per_line": 20, "language_defaults": False},
    )
    seed_workspace_setting(
        case.database,
        other_workspace_id,
        "defaults.stt",
        {"subtitle_max_lines": 8},
    )
    seed_workspace_setting(
        case.database,
        other_workspace_id,
        "defaults.translation",
        {"target_language": "it"},
    )
    with case.database.immediate_session() as session:
        session_record = session.get(SessionRecord, session_id)
        session_record.workflow_kind = "subtitles"
        session_record.source_language = "en"
        session_record.target_language = "pl"
        session.add_all(
            [
                SessionSetting(
                    session_id=session_id,
                    section="subtitles",
                    value_json={"max_chars_per_line": 37, "language_defaults": False},
                    revision=5,
                ),
                SessionSetting(
                    session_id=session_id,
                    section="stt",
                    value_json={
                        "subtitle_max_chars_per_line": 50,
                        "subtitle_max_lines": 3,
                        "subtitle_max_cps": 15,
                    },
                    revision=6,
                ),
                SessionSetting(
                    session_id=session_id,
                    section="translation",
                    value_json={"target_language": "fr"},
                    revision=7,
                ),
            ]
        )

    reads = {"workspace_settings": 0, "session_settings": 0, "outcome_plans": 0}

    def count_composition_reads(
        _connection, _cursor, statement, _parameters, _context, _executemany
    ) -> None:
        normalized = statement.lower()
        for table in reads:
            if f"from {table}" in normalized:
                reads[table] += 1

    event.listen(case.database.engine, "before_cursor_execute", count_composition_reads)
    try:
        absent_snapshot = service.get(session_id, "subtitles")
    finally:
        event.remove(case.database.engine, "before_cursor_execute", count_composition_reads)

    assert reads == {"workspace_settings": 1, "session_settings": 1, "outcome_plans": 1}
    assert absent_snapshot["workspace_id"] == workspace_id
    assert absent_snapshot["global"] == {
        "max_chars_per_line": 42,
        "max_cps": 19,
        "language_defaults": True,
        "max_lines": 2,
    }
    assert absent_snapshot["override"] == {
        "max_chars_per_line": 37,
        "language_defaults": False,
        "max_lines": 3,
        "max_cps": 15,
    }
    expected_effective = {
        **BUILTIN_DEFAULTS["subtitles"],
        **absent_snapshot["global"],
        **absent_snapshot["override"],
    }
    assert absent_snapshot["effective"] == expected_effective
    assert absent_snapshot["global_revision"] == 2
    assert absent_snapshot["revision"] == 5
    assert absent_snapshot["subtitle_profiles"]["source"]["language"] == "en"
    assert absent_snapshot["subtitle_profiles"]["target"]["language"] == "fr"
    assert (
        absent_snapshot["subtitle_profiles"]["target"]["limits"]["max_chars_per_line"]["effective"]
        == 37
    )

    absent_resolved, absent_hash = service.resolve(session_id, sections=["subtitles"])
    assert absent_resolved == {"subtitles": expected_effective}
    assert absent_hash == stable_hash(absent_resolved)

    with case.database.immediate_session() as session:
        session.add(
            OutcomePlan(
                session_id=session_id,
                value_json={"inputs": {"generation": "source"}},
            )
        )

    present_snapshot, present_resolved, present_hash = _read_subtitle_composition(case, session_id)
    assert present_snapshot == absent_snapshot
    assert present_resolved == absent_resolved
    assert present_hash == absent_hash

    with case.database.immediate_session() as session:
        global_subtitles = session.get(WorkspaceSetting, (workspace_id, "defaults.subtitles"))
        global_translation = session.get(WorkspaceSetting, (workspace_id, "defaults.translation"))
        override_subtitles = session.get(SessionSetting, (session_id, "subtitles"))
        override_translation = session.get(SessionSetting, (session_id, "translation"))
        global_subtitles.value_json = {
            "max_chars_per_line": 46,
            "max_cps": 22,
            "language_defaults": True,
        }
        global_subtitles.revision += 1
        global_translation.value_json = {"target_language": "it"}
        override_subtitles.value_json = {
            "max_chars_per_line": 33,
            "language_defaults": False,
        }
        override_subtitles.revision += 1
        override_translation.value_json = {"target_language": "es"}

    updated_snapshot, updated_resolved, updated_hash = _read_subtitle_composition(case, session_id)
    assert updated_snapshot["global"]["max_chars_per_line"] == 46
    assert updated_snapshot["override"]["max_chars_per_line"] == 33
    assert updated_snapshot["effective"]["max_chars_per_line"] == 33
    assert updated_snapshot["subtitle_profiles"]["target"]["language"] == "es"
    assert updated_resolved == {"subtitles": updated_snapshot["effective"]}
    assert updated_hash == stable_hash(updated_resolved)
    assert updated_hash != absent_hash


def test_subtitle_composition_preserves_pending_existing_override(
    settings_case: SettingsCase,
) -> None:
    case = settings_case
    session_id = case.sessions["alpha"]
    service = WorkspaceSettingsService(case.database)
    original = {"max_chars_per_line": 48, "language_defaults": True}
    pending = {"max_chars_per_line": 33, "language_defaults": False}
    with case.database.immediate_session() as session:
        session.add_all(
            [
                SessionSetting(
                    session_id=session_id,
                    section="subtitles",
                    value_json=original,
                    revision=2,
                ),
                OutcomePlan(
                    session_id=session_id,
                    value_json={"inputs": {"generation": "source"}},
                ),
            ]
        )

    with case.database.immediate_session() as session:
        session_record = session.get(SessionRecord, session_id)
        outcome_plan = session.get(OutcomePlan, session_id)
        assert session_record is not None
        assert outcome_plan is not None
        override = session.get(SessionSetting, (session_id, "subtitles"))
        override.value_json = pending
        override.revision = 9
        snapshot = service.get_in_session(session, session_id, "subtitles")
        assert snapshot["override"] == pending
        assert snapshot["revision"] == 9

    with case.database.snapshot_session() as session:
        persisted = session.get(SessionSetting, (session_id, "subtitles"))
        assert persisted.value_json == pending
        assert persisted.revision == 9


def test_subtitle_composition_includes_pending_new_translation_override(
    settings_case: SettingsCase,
) -> None:
    case = settings_case
    session_id = case.sessions["alpha"]
    service = WorkspaceSettingsService(case.database)
    with case.database.immediate_session() as session:
        session_record = session.get(SessionRecord, session_id)
        session_record.workflow_kind = "subtitles"
        session_record.source_language = "en"
        session_record.target_language = "pl"
        session.add(
            SessionSetting(
                session_id=session_id,
                section="subtitles",
                value_json={"max_chars_per_line": 37},
                revision=1,
            )
        )
        session.add(
            OutcomePlan(
                session_id=session_id,
                value_json={"inputs": {"generation": "source"}},
            )
        )

    with case.database.immediate_session() as session:
        session_record = session.get(SessionRecord, session_id)
        outcome_plan = session.get(OutcomePlan, session_id)
        assert session_record is not None
        assert outcome_plan is not None
        pending = SessionSetting(
            session_id=session_id,
            section="translation",
            value_json={"target_language": "es"},
            revision=1,
        )
        session.add(pending)
        snapshot = service.get_in_session(session, session_id, "subtitles")
        assert snapshot["subtitle_profiles"]["target"]["language"] == "es"

    with case.database.snapshot_session() as session:
        persisted = session.get(SessionSetting, (session_id, "translation"))
        assert persisted.value_json == {"target_language": "es"}
        assert persisted.revision == 1
