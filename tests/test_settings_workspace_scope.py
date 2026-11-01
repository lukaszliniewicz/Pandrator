"""Workspace-scoped defaults and session settings resolution."""

from __future__ import annotations

from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from threading import Barrier
from typing import Any
from unittest.mock import patch

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import event, select

from pandrator.runtime import DataPaths
from pandrator.web.accounts.identity import initial_personal_workspace
from pandrator.web.accounts.models import Workspace
from pandrator.web.database import Database, sqlite_url
from pandrator.web.global_settings import (
    GlobalSettingsService,
    SettingPreconditionError,
)
from pandrator.web.models import (
    AppSetting,
    Project,
    SessionRecord,
    SessionSetting,
    WorkspaceSetting,
    WorkspaceSettingHistory,
)
from pandrator.web.sessions import SessionService
from pandrator.web.settings_identity import settings_workspace_id
from pandrator.web.workspace_settings import WorkspaceSettingsService
from tests.web_test_support import prepare_web_test_data_root


@dataclass(frozen=True)
class SettingsCase:
    database: Database
    paths: DataPaths
    workspaces: dict[str, str]
    sessions: dict[str, str]


@pytest.fixture
def settings_case(tmp_path: Path) -> Iterator[SettingsCase]:
    paths = prepare_web_test_data_root(tmp_path)
    database = Database(paths.database)
    workspaces: dict[str, str] = {}
    with database.immediate_session() as session:
        workspaces["personal"] = initial_personal_workspace(session).id
        for name in ("alpha", "beta", "gamma"):
            workspace = Workspace(kind="team", name=name)
            session.add(workspace)
            session.flush()
            workspaces[name] = workspace.id
    sessions = {
        name: SessionService(database).create(name, workspace_id=workspaces[name]).id
        for name in ("alpha", "beta", "gamma")
    }
    try:
        yield SettingsCase(database, paths, workspaces, sessions)
    finally:
        database.dispose()


def seed_workspace_setting(
    database: Database,
    workspace_id: str,
    key: str,
    value: dict[str, Any],
    *,
    revision: int = 1,
) -> None:
    with database.immediate_session() as session:
        session.add(
            WorkspaceSetting(
                workspace_id=workspace_id,
                key=key,
                value_json=value,
                revision=revision,
            )
        )


def test_global_settings_revisions_and_history_are_workspace_scoped(
    settings_case: SettingsCase,
) -> None:
    case = settings_case
    settings = GlobalSettingsService(case.database, case.paths)
    alpha = case.workspaces["alpha"]
    beta = case.workspaces["beta"]

    settings.replace("custom.private", {"owner": "alpha"}, "0", workspace_id=alpha)
    settings.replace("custom.private", {"owner": "beta"}, "0", workspace_id=beta)
    assert settings.get("custom.private", workspace_id=alpha)["value"] == {"owner": "alpha"}
    assert settings.get("custom.private", workspace_id=beta)["value"] == {"owner": "beta"}

    updated = settings.replace(
        "custom.private", {"owner": "alpha-updated"}, "1", workspace_id=alpha
    )
    assert updated["revision"] == 2
    assert settings.get("custom.private", workspace_id=beta)["revision"] == 1
    with case.database.snapshot_session() as session:
        history = session.scalars(
            select(WorkspaceSettingHistory).where(
                WorkspaceSettingHistory.workspace_id == alpha,
                WorkspaceSettingHistory.key == "custom.private",
            )
        ).all()
        assert [(row.revision, row.value_json) for row in history] == [(1, {"owner": "alpha"})]
        assert session.get(AppSetting, "custom.private") is None


def test_service_credential_preparation_receives_the_validated_workspace(
    settings_case: SettingsCase,
) -> None:
    case = settings_case
    settings = GlobalSettingsService(case.database, case.paths)
    alpha = case.workspaces["alpha"]
    beta = case.workspaces["beta"]
    resolved_owners: dict[str, str] = {}

    def prepare_tts(*args: Any, workspace_id: str) -> Any:
        resolved_owners["tts"] = workspace_id
        return args[3]

    def prepare_stt(*args: Any, workspace_id: str) -> Any:
        resolved_owners["stt"] = workspace_id
        return args[3]

    with (
        patch(
            "pandrator.web.global_settings.prepare_tts_settings_for_storage",
            side_effect=prepare_tts,
        ),
        patch(
            "pandrator.web.global_settings.prepare_stt_settings_for_storage",
            side_effect=prepare_stt,
        ),
    ):
        settings.replace("services.tts", {"service": "XTTS"}, "0", workspace_id=alpha)
        settings.replace("services.stt", {"stt_engine": "whisper"}, "0", workspace_id=beta)

    assert resolved_owners == {"tts": alpha, "stt": beta}
    assert settings.get("services.tts", workspace_id=alpha)["value"] == {"service": "XTTS"}
    assert settings.get("services.stt", workspace_id=beta)["value"] == {"stt_engine": "whisper"}


def test_defaults_use_workspace_rows_and_empty_workspace_builtin_fallback(
    settings_case: SettingsCase,
) -> None:
    case = settings_case
    settings = GlobalSettingsService(case.database, case.paths)
    for owner, marker in (("alpha", "alpha"), ("beta", "beta")):
        seed_workspace_setting(
            case.database,
            case.workspaces[owner],
            "defaults.text",
            {"marker": marker},
        )
    with case.database.immediate_session() as session:
        session.add(
            AppSetting(
                key="defaults.text",
                value_json={"marker": "legacy-host"},
                revision=7,
            )
        )

    alpha = settings.defaults("text", workspace_id=case.workspaces["alpha"])
    beta = settings.defaults("text", workspace_id=case.workspaces["beta"])
    gamma = settings.defaults("text", workspace_id=case.workspaces["gamma"])
    assert alpha["value"] == {"marker": "alpha"}
    assert beta["value"] == {"marker": "beta"}
    assert gamma["value"] == {}
    assert gamma["effective"] == gamma["builtin"]


def test_noninitial_host_keys_fail_before_app_setting_values_are_read(
    settings_case: SettingsCase,
) -> None:
    case = settings_case
    service = GlobalSettingsService(case.database, case.paths)
    with case.database.immediate_session() as session:
        session.add(
            AppSetting(
                key="system.identity",
                value_json={"marker": "host-only"},
                revision=1,
            )
        )

    statements: list[str] = []

    def record_statement(_connection, _cursor, statement, _parameters, _context, _many):
        statements.append(statement.lower())

    event.listen(case.database.engine, "before_cursor_execute", record_statement)
    try:
        with pytest.raises(ValueError, match="initial personal workspace"):
            service.get("system.identity", workspace_id=case.workspaces["alpha"])
        assert not any("app_settings" in statement for statement in statements)

        statements.clear()
        with pytest.raises(ValueError, match="initial personal workspace"):
            service.replace(
                "system.identity",
                {"marker": "must-not-write"},
                "1",
                workspace_id=case.workspaces["alpha"],
            )
        assert not any("app_settings" in statement for statement in statements)
    finally:
        event.remove(case.database.engine, "before_cursor_execute", record_statement)

    assert service.get("system.identity")["value"] == {"marker": "host-only"}


def test_service_resolution_inherits_workspace_defaults_and_actual_session_override(
    settings_case: SettingsCase,
) -> None:
    case = settings_case
    for owner, marker, sentence_length in (
        ("alpha", "alpha-service", 123),
        ("beta", "beta-service", 222),
    ):
        workspace_id = case.workspaces[owner]
        seed_workspace_setting(
            case.database,
            workspace_id,
            "defaults.text",
            {"max_sentence_length": sentence_length},
        )
        seed_workspace_setting(
            case.database,
            workspace_id,
            "services.stt",
            {
                "stt_engine": "whisper",
                "provider_configs": [{"id": "whisper", "settings": {"workspace_marker": marker}}],
            },
        )
    with case.database.immediate_session() as session:
        session.add(
            SessionSetting(
                session_id=case.sessions["alpha"],
                section="text",
                value_json={
                    "audiobook_chunking": "manual",
                    "max_sentence_length": 77,
                },
                revision=1,
            )
        )

    settings = WorkspaceSettingsService(case.database)
    alpha, _ = settings.resolve(case.sessions["alpha"], sections=["text", "stt", "subtitles"])
    beta, _ = settings.resolve(case.sessions["beta"], sections=["text", "stt"])
    assert alpha["text"]["max_sentence_length"] == 77
    assert alpha["text"]["audiobook_chunking"] == "manual"
    assert beta["text"]["max_sentence_length"] == 222
    assert beta["text"]["audiobook_chunking"] == "manual"
    assert alpha["stt"]["workspace_marker"] == "alpha-service"
    assert beta["stt"]["workspace_marker"] == "beta-service"


def test_workspace_resolution_validates_session_root_once_for_all_sections(
    settings_case: SettingsCase,
) -> None:
    case = settings_case
    settings = WorkspaceSettingsService(case.database)
    with patch(
        "pandrator.web.workspace_settings.settings_workspace_id",
        wraps=settings_workspace_id,
    ) as resolve_owner:
        settings.resolve(case.sessions["alpha"], sections=["text", "stt", "subtitles"])
    assert resolve_owner.call_count == 1


def test_stale_cached_project_root_uses_current_workspace_defaults(
    settings_case: SettingsCase,
) -> None:
    case = settings_case
    # Retain the defensive reader check on a genuine pre-immutability schema.
    config = Config()
    config.set_main_option("script_location", str(Path(__file__).parents[1] / "pandrator/web/migrations"))
    config.set_main_option("sqlalchemy.url", sqlite_url(case.database.path))
    command.downgrade(config, "0075_mandatory_ownership")
    alpha = case.workspaces["alpha"]
    beta = case.workspaces["beta"]
    seed_workspace_setting(case.database, alpha, "defaults.text", {"marker": "alpha"})
    seed_workspace_setting(case.database, beta, "defaults.text", {"marker": "beta"})

    with case.database.session() as session:
        record = session.get(SessionRecord, case.sessions["alpha"])
        assert record is not None
        project = session.get(Project, record.project_id)
        assert project is not None and project.workspace_id == alpha
        with case.database.immediate_session() as writer:
            current_project = writer.get(Project, project.id)
            assert current_project is not None
            current_project.workspace_id = beta

        snapshot = WorkspaceSettingsService(case.database).get_in_session(
            session, case.sessions["alpha"], "text"
        )
        assert snapshot["global"] == {"marker": "beta"}


def test_post_write_root_failure_rolls_back_the_whole_caller_transaction(
    settings_case: SettingsCase,
) -> None:
    case = settings_case
    workspace_id = case.workspaces["alpha"]
    session_id = case.sessions["alpha"]
    settings = WorkspaceSettingsService(case.database)
    with case.database.session() as session:
        session.add(
            WorkspaceSetting(
                workspace_id=case.workspaces["beta"],
                key="pending.marker",
                value_json={"pending": True},
                revision=1,
            )
        )
        with patch(
            "pandrator.web.workspace_settings._session_workspace_id",
            side_effect=[workspace_id, ValueError("stale session root")],
        ):
            with pytest.raises(ValueError, match="stale session root"):
                settings.update_in_session(
                    session,
                    session_id,
                    "text",
                    0,
                    {"audiobook_chunking": "manual", "max_sentence_length": 90},
                )

    with case.database.snapshot_session() as session:
        assert (
            session.scalar(
                select(WorkspaceSetting.key).where(
                    WorkspaceSetting.workspace_id == case.workspaces["beta"],
                    WorkspaceSetting.key == "pending.marker",
                )
            )
            is None
        )
        assert session.get(SessionSetting, (session_id, "text")) is None


def test_default_stt_subtitle_migration_writes_history_in_selected_workspace(
    settings_case: SettingsCase,
) -> None:
    case = settings_case
    alpha = case.workspaces["alpha"]
    beta = case.workspaces["beta"]
    seed_workspace_setting(case.database, alpha, "defaults.subtitles", {"max_lines": 4}, revision=1)
    seed_workspace_setting(case.database, beta, "defaults.subtitles", {"max_lines": 2}, revision=1)
    service = GlobalSettingsService(case.database, case.paths)
    service.replace(
        "defaults.stt",
        {"stt_engine": "whisper", "subtitle_max_chars_per_line": 77},
        "0",
        workspace_id=alpha,
    )

    with case.database.snapshot_session() as session:
        alpha_subtitles = session.get(WorkspaceSetting, (alpha, "defaults.subtitles"))
        beta_subtitles = session.get(WorkspaceSetting, (beta, "defaults.subtitles"))
        assert alpha_subtitles is not None
        assert alpha_subtitles.revision == 2
        assert alpha_subtitles.value_json == {
            "max_lines": 4,
            "max_chars_per_line": 77,
            "language_defaults": False,
        }
        assert beta_subtitles is not None
        assert beta_subtitles.revision == 1
        assert beta_subtitles.value_json == {"max_lines": 2}
        history = session.scalars(
            select(WorkspaceSettingHistory).where(
                WorkspaceSettingHistory.workspace_id == alpha,
                WorkspaceSettingHistory.key == "defaults.subtitles",
            )
        ).all()
        assert [(item.revision, item.value_json) for item in history] == [(1, {"max_lines": 4})]


def test_concurrent_workspace_writers_keep_optimistic_revisions_isolated(
    settings_case: SettingsCase,
) -> None:
    case = settings_case
    settings = GlobalSettingsService(case.database, case.paths)
    alpha = case.workspaces["alpha"]
    beta = case.workspaces["beta"]
    for workspace_id in (alpha, beta):
        settings.replace("custom.concurrent", {"writer": "seed"}, "0", workspace_id=workspace_id)

    def concurrent_updates(workspace_ids: tuple[str, str], revision: str):
        barrier = Barrier(2)

        def replace(workspace_id: str, value: str):
            barrier.wait(timeout=10)
            try:
                return settings.replace(
                    "custom.concurrent",
                    {"writer": value},
                    revision,
                    workspace_id=workspace_id,
                )
            except SettingPreconditionError as error:
                return error

        with ThreadPoolExecutor(max_workers=2) as executor:
            return list(
                executor.map(
                    lambda item: replace(*item),
                    ((workspace_ids[0], "first"), (workspace_ids[1], "second")),
                )
            )

    independent = concurrent_updates((alpha, beta), "1")
    assert all(isinstance(result, dict) for result in independent)
    assert settings.get("custom.concurrent", workspace_id=alpha)["revision"] == 2
    assert settings.get("custom.concurrent", workspace_id=beta)["revision"] == 2

    contended = concurrent_updates((alpha, alpha), "2")
    assert sum(isinstance(result, dict) for result in contended) == 1
    conflicts = [result for result in contended if isinstance(result, SettingPreconditionError)]
    assert len(conflicts) == 1
    assert conflicts[0].status_code == 409
    assert settings.get("custom.concurrent", workspace_id=alpha)["revision"] == 3
