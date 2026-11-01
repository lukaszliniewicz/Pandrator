"""Settings readers and admission fingerprints follow the actual workspace."""

from __future__ import annotations

import os
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from sqlalchemy import select

from pandrator.runtime import DataPaths
from pandrator.web import subtitle_evidence
from pandrator.web.accounts.identity import initial_personal_workspace
from pandrator.web.credentials import (
    database_reference,
    stt_service_credential_key,
    tts_service_credential_key,
    upsert_credential,
)
from pandrator.web.database import Database
from pandrator.web.generation_admission import _settings_revision_guard
from pandrator.web.google_voice_design import configured_google_design_service
from pandrator.web.maintenance import apply_retention
from pandrator.web.manager_proxy import LocalManagerProxy
from pandrator.web.models import (
    AppSettingHistory,
    SessionRecord,
    Workspace,
    WorkspaceSetting,
    WorkspaceSettingHistory,
    utcnow,
)
from pandrator.web.sessions import SessionService
from pandrator.web.stt_providers import stt_catalogue_snapshot
from pandrator.web.tts_catalogue_service import TtsCatalogueProviders, TtsCatalogueService
from pandrator.web.workflow_plans import WorkflowExecutionPlanService
from tests.web_test_support import prepare_web_test_data_root


@dataclass(frozen=True)
class SettingsCase:
    database: Database
    paths: DataPaths
    workspaces: dict[str, str]
    sessions: dict[str, SessionRecord]

    def put(self, owner: str, key: str, value: dict[str, Any], *, revision: int = 1) -> None:
        with self.database.immediate_session() as session:
            record = session.get(WorkspaceSetting, (self.workspaces[owner], key))
            if record is None:
                session.add(
                    WorkspaceSetting(
                        workspace_id=self.workspaces[owner],
                        key=key,
                        value_json=value,
                        revision=revision,
                    )
                )
            else:
                record.value_json = value
                record.revision = revision


@pytest.fixture
def settings_case(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[SettingsCase]:
    monkeypatch.setattr(os, "environ", {})
    paths = prepare_web_test_data_root(tmp_path)
    database = Database(paths.database)
    with database.immediate_session() as session:
        workspaces = {"personal": initial_personal_workspace(session).id}
        for owner in ("alpha", "beta", "empty"):
            workspace = Workspace(kind="team", name=owner)
            session.add(workspace)
            session.flush()
            workspaces[owner] = workspace.id
    service = SessionService(database)
    sessions = {
        name: service.create(name, workspace_id=identity, seed_voice_mode=False)
        for name, identity in workspaces.items()
    }
    try:
        yield SettingsCase(database, paths, workspaces, sessions)
    finally:
        database.dispose()


def test_tts_catalogue_discovery_and_preview_use_the_selected_workspace(
    settings_case: SettingsCase,
) -> None:
    case = settings_case
    key = tts_service_credential_key("openai")
    for owner, language in (("alpha", "de"), ("beta", "pl")):
        case.put(
            owner,
            "services.tts",
            {
                "provider_configs": [
                    {
                        "id": "openai",
                        "api_base": f"https://{owner}.invalid/v1",
                        "secret_ref": database_reference(key),
                        "default_model": f"{owner}-model",
                        "models": [f"{owner}-model"],
                    }
                ]
            },
            revision=5,
        )
        case.put(owner, "defaults.tts", {"language": language}, revision=7)
        with case.database.immediate_session() as session:
            upsert_credential(
                session,
                key,
                "Synthetic",
                f"synthetic-{owner}-tts",
                workspace_id=case.workspaces[owner],
            )
    catalogue = TtsCatalogueService(
        case.database,
        case.paths,
        cast(TtsCatalogueProviders, SimpleNamespace()),
        manager_bridge=cast(LocalManagerProxy, SimpleNamespace(configured=False)),
    )
    for owner, language in (("alpha", "de"), ("beta", "pl")):
        connections, revision, defaults, default_revision = catalogue._settings(
            workspace_id=case.workspaces[owner]
        )
        assert (revision, default_revision) == (5, 7)
        assert defaults == {"language": language}
        assert connections["provider_configs"][0]["api_base"] == f"https://{owner}.invalid/v1"
        assert (
            catalogue.discovery_api_key("openai", workspace_id=case.workspaces[owner])
            == f"synthetic-{owner}-tts"
        )
        preview = catalogue.preview_settings(
            "openai", model=None, voice=None, language=None, workspace_id=case.workspaces[owner]
        )
        assert preview is not None
        assert preview["model"] == f"{owner}-model"
        assert preview["language"] == language
        assert f"synthetic-{owner}-tts" not in repr(preview)
    assert catalogue._settings(workspace_id=case.workspaces["empty"]) == ({}, 0, {}, 0)


def test_stt_catalogue_and_evidence_readiness_do_not_inherit_other_connections(
    settings_case: SettingsCase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = settings_case
    key = stt_service_credential_key("azure_mai_transcribe_2")
    case.put(
        "alpha",
        "services.stt",
        {
            "provider_configs": [
                {
                    "id": "azure_mai_transcribe_2",
                    "secret_ref": database_reference(key),
                    "api_base": "https://alpha.invalid",
                    "owner_marker": "alpha-only",
                }
            ]
        },
        revision=4,
    )
    with case.database.immediate_session() as session:
        upsert_credential(
            session, key, "Synthetic", "synthetic-alpha-stt", workspace_id=case.workspaces["alpha"]
        )
    monkeypatch.setattr(subtitle_evidence, "_cached_stt_backend_statuses", lambda: {})
    for owner, ready in (("alpha", True), ("beta", False)):
        payload, revision = stt_catalogue_snapshot(
            case.database, case.paths, session_id=case.sessions[owner].id
        )
        assert revision == (4 if ready else 0)
        profile = next(
            item for item in payload["profiles"] if item["id"] == "azure_mai_transcribe_2"
        )
        assert profile["credential_configured"] is ready
        assert "synthetic-alpha-stt" not in repr(payload)
        if not ready:
            assert payload["services"] == []
            assert "alpha-only" not in repr(payload)
        routes = subtitle_evidence.evidence_route_catalog(
            None,
            database=case.database,
            paths=case.paths,
            session_id=case.sessions[owner].id,
        )
        route = next(item for item in routes if item["route"] == "azure_mai_transcribe_2")
        assert route["ready"] is ready


def test_google_design_service_reads_the_voice_workspace_connection_and_key(
    settings_case: SettingsCase,
) -> None:
    case = settings_case
    key = tts_service_credential_key("gemini")
    for owner in ("alpha", "beta"):
        case.put(
            owner,
            "services.tts",
            {
                "provider_configs": [
                    {
                        "id": "gemini",
                        "secret_ref": database_reference(key),
                        "api_base": f"https://{owner}.invalid",
                        "models": [f"{owner}-model"],
                    }
                ]
            },
        )
        with case.database.immediate_session() as session:
            upsert_credential(
                session,
                key,
                "Synthetic",
                f"synthetic-{owner}-google",
                workspace_id=case.workspaces[owner],
            )
        service, credential = configured_google_design_service(
            case.database, case.paths, "gemini", workspace_id=case.workspaces[owner]
        )
        assert service["api_base"] == f"https://{owner}.invalid"
        assert credential == f"synthetic-{owner}-google"


def test_settings_fingerprints_ignore_other_workspaces_and_include_stt_connections(
    settings_case: SettingsCase,
) -> None:
    case = settings_case
    sid = case.sessions["alpha"].id
    case.put("alpha", "defaults.tts", {"language": "de"})
    case.put("beta", "defaults.tts", {"language": "pl"})

    def fingerprints() -> tuple[str, dict[str, Any]]:
        with case.database.snapshot_session() as session:
            return (
                _settings_revision_guard(session, sid),
                WorkflowExecutionPlanService._state_snapshot(session, sid),
            )

    original = fingerprints()
    case.put("beta", "defaults.tts", {"language": "es"}, revision=2)
    case.put("beta", "services.stt", {"owner_marker": "beta-only"})
    assert fingerprints() == original
    case.put("alpha", "services.stt", {"owner_marker": "alpha-only"})
    changed = fingerprints()
    assert changed[0] != original[0]
    assert changed[1] != original[1]
    assert "beta-only" not in repr(changed)


def test_retention_cleans_both_settings_histories_without_deleting_current_values(
    settings_case: SettingsCase,
) -> None:
    case = settings_case
    old = utcnow() - timedelta(days=100)
    case.put("alpha", "defaults.text", {"max_sentence_length": 350})
    with case.database.immediate_session() as session:
        session.add(
            AppSettingHistory(key="web.preferences", value_json={}, revision=1, created_at=old)
        )
        for owner in ("alpha", "beta"):
            session.add(
                WorkspaceSettingHistory(
                    workspace_id=case.workspaces[owner],
                    key="defaults.text",
                    value_json={},
                    revision=1,
                    created_at=old,
                )
            )
    result = apply_retention(case.database, case.paths, 30)
    assert result["app_setting_history"] == 1
    assert result["workspace_setting_history"] == 2
    with case.database.snapshot_session() as session:
        assert session.scalars(select(WorkspaceSettingHistory)).all() == []
        current = session.get(WorkspaceSetting, (case.workspaces["alpha"], "defaults.text"))
        assert current is not None and current.value_json == {"max_sentence_length": 350}
