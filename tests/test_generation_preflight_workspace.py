"""Workspace-scoped, read-only generation provider preflight coverage."""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import patch

import pytest
import requests
from sqlalchemy import event, func, select, update

from pandrator.logic import tts_handler
from pandrator.runtime import DataPaths
from pandrator.web.accounts.identity import initial_personal_workspace
from pandrator.web.accounts.models import Workspace
from pandrator.web.credentials import (
    shared_provider_credential_key,
    upsert_credential,
)
from pandrator.web.database import Database
from pandrator.web.generation_provider_preflight import generation_provider_preflight
from pandrator.web.models import SessionRecord, StoredCredential
from pandrator.web.projects.identity import create_session_project
from tests.web_test_support import prepare_web_test_data_root

_WORKSPACE_WARNING = "Choose a speech connection configured for this workspace."


@dataclass(frozen=True)
class PreflightCase:
    database: Database
    root: Path
    personal_workspace_id: str
    team_workspace_id: str
    personal_session_id: str
    team_session_id: str


@pytest.fixture
def preflight_case(tmp_path: Path) -> Iterator[PreflightCase]:
    paths = prepare_web_test_data_root(tmp_path)
    database = Database(paths.database)
    with database.immediate_session() as session:
        personal = initial_personal_workspace(session)
        team = Workspace(kind="team", name="Preflight team")
        session.add(team)
        session.flush()

        personal_record = SessionRecord(name="Personal preflight", storage_key="personal-preflight")
        create_session_project(session, personal_record, workspace_id=personal.id)
        team_record = SessionRecord(name="Team preflight", storage_key="team-preflight")
        create_session_project(session, team_record, workspace_id=team.id)
        personal_workspace_id = personal.id
        team_workspace_id = team.id
        personal_session_id = personal_record.id
        team_session_id = team_record.id

    try:
        yield PreflightCase(
            database=database,
            root=paths.root,
            personal_workspace_id=personal_workspace_id,
            team_workspace_id=team_workspace_id,
            personal_session_id=personal_session_id,
            team_session_id=team_session_id,
        )
    finally:
        database.dispose()


@pytest.fixture(autouse=True)
def block_provider_access(monkeypatch: pytest.MonkeyPatch) -> None:
    def blocked_request(*_args: object, **_kwargs: object) -> None:
        pytest.fail("Generation preflight must not make an HTTP request.")

    monkeypatch.setattr(requests.sessions.Session, "request", blocked_request)
    monkeypatch.setattr(
        tts_handler,
        "_vertex_access_token",
        lambda *_args, **_kwargs: pytest.fail(
            "Generation preflight must not probe Vertex authentication."
        ),
    )


def _vertex_snapshot(
    *, secret_ref: str = "db:shared:vertex_ai", project: str = "", api_key_env: str = ""
) -> dict[str, object]:
    return {
        "tts": {
            "service": "vertex_ai",
            "model": "gemini-3.8-flash-tts",
            "speaker": "Kore",
            "provider_configs": [
                {
                    "id": "vertex_ai",
                    "secret_ref": secret_ref,
                    "api_key_env": api_key_env,
                    "vertex_project": project,
                    "vertex_location": "global",
                }
            ],
        }
    }


def _store_credential(case: PreflightCase, workspace_id: str, value: str) -> None:
    with case.database.session() as session:
        upsert_credential(
            session,
            shared_provider_credential_key("vertex_ai"),
            "Vertex AI",
            value,
            workspace_id=workspace_id,
        )


def test_same_service_uses_credentials_from_each_current_session_root(
    preflight_case: PreflightCase,
) -> None:
    case = preflight_case
    _store_credential(
        case,
        case.personal_workspace_id,
        json.dumps({"type": "service_account", "project_id": "personal-project"}),
    )
    _store_credential(
        case,
        case.team_workspace_id,
        json.dumps({"type": "service_account", "project_id": "team-project"}),
    )

    personal = generation_provider_preflight(
        case.database, _vertex_snapshot(), session_id=case.personal_session_id
    )
    team = generation_provider_preflight(
        case.database, _vertex_snapshot(), session_id=case.team_session_id
    )

    assert personal["credential_origin"] == team["credential_origin"] == "database"
    assert personal["credential_configured"] is team["credential_configured"] is True
    assert personal["project"] == "personal-project"
    assert team["project"] == "team-project"
    assert personal["project_resolution"] == team["project_resolution"] == "credential"
    assert personal["access_checked"] is team["access_checked"] is False


def test_unconfigured_team_does_not_borrow_personal_environment_or_adc(
    preflight_case: PreflightCase, monkeypatch: pytest.MonkeyPatch
) -> None:
    case = preflight_case
    _store_credential(
        case,
        case.personal_workspace_id,
        json.dumps({"type": "service_account", "project_id": "personal-only-project"}),
    )
    monkeypatch.setenv("PREFLIGHT_GEMINI_KEY_SENTINEL", "host-gemini-secret")
    monkeypatch.setenv("VERTEX_SERVICE_ACCOUNT_JSON", "host-vertex-secret")
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "host-project-sentinel")
    monkeypatch.setenv("GCLOUD_PROJECT", "fallback-project-sentinel")
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", "/tmp/host-adc-file-sentinel.json")

    vertex = generation_provider_preflight(
        case.database,
        _vertex_snapshot(secret_ref="", api_key_env="VERTEX_SERVICE_ACCOUNT_JSON"),
        session_id=case.team_session_id,
    )
    gemini_snapshot: dict[str, object] = {
        "tts": {
            "service": "gemini",
            "model": "gemini-2.5-flash",
            "speaker": "Kore",
            "provider_configs": [
                {
                    "id": "gemini",
                    "secret_ref": "",
                    "api_key_env": "PREFLIGHT_GEMINI_KEY_SENTINEL",
                }
            ],
        }
    }
    gemini = generation_provider_preflight(
        case.database, gemini_snapshot, session_id=case.team_session_id
    )

    assert vertex["credential_origin"] == gemini["credential_origin"] == "unconfigured"
    assert vertex["credential_configured"] is gemini["credential_configured"] is False
    assert vertex["project"] == ""
    assert vertex["project_resolution"] == "unavailable"
    assert vertex["configuration_warning"] == gemini["configuration_warning"] == _WORKSPACE_WARNING
    assert "host-project-sentinel" not in repr(vertex)
    assert "fallback-project-sentinel" not in repr(vertex)
    assert "host-vertex-secret" not in repr(vertex)
    assert "/tmp/host-adc-file-sentinel.json" not in repr(vertex)
    assert "host-gemini-secret" not in repr(gemini)


@pytest.mark.parametrize(
    "secret_ref",
    (
        "env:PREFLIGHT_GEMINI_KEY_SENTINEL",
        "keyring:Pandrator/preflight-sentinel",
        "file-path:/tmp/pandrator-preflight-never-read.json",
    ),
)
def test_team_rejects_ambient_credential_references_before_backend_access(
    preflight_case: PreflightCase,
    monkeypatch: pytest.MonkeyPatch,
    secret_ref: str,
) -> None:
    case = preflight_case
    monkeypatch.setenv("PREFLIGHT_GEMINI_KEY_SENTINEL", "host-gemini-secret")
    with (
        patch(
            "pandrator.web.credentials._load_keyring",
            side_effect=AssertionError("keyring backend must not be opened"),
        ),
        patch(
            "pandrator.web.credentials._read_secret_file",
            side_effect=AssertionError("secret file must not be read"),
        ),
    ):
        result = generation_provider_preflight(
            case.database,
            _vertex_snapshot(secret_ref=secret_ref),
            workspace_id=case.team_workspace_id,
        )

    assert result["credential_origin"] == "unavailable"
    assert result["credential_configured"] is False
    assert result["project"] == ""
    assert result["project_resolution"] == "unavailable"
    assert result["configuration_warning"] == _WORKSPACE_WARNING
    assert secret_ref not in repr(result)
    assert "host-gemini-secret" not in repr(result)


def test_team_vertex_json_project_is_used_and_explicit_project_wins(
    preflight_case: PreflightCase,
) -> None:
    case = preflight_case
    _store_credential(
        case,
        case.team_workspace_id,
        json.dumps({"type": "service_account", "project_id": "team-json-project"}),
    )

    from_credential = generation_provider_preflight(
        case.database, _vertex_snapshot(), session_id=case.team_session_id
    )
    from_service = generation_provider_preflight(
        case.database,
        _vertex_snapshot(project="explicit-service-project"),
        session_id=case.team_session_id,
    )

    assert from_credential["project"] == "team-json-project"
    assert from_credential["project_resolution"] == "credential"
    assert from_service["project"] == "explicit-service-project"
    assert from_service["project_resolution"] == "service"
    assert from_service["credential_origin"] == "database"


def test_supplied_roots_are_validated_before_credential_resolution(
    preflight_case: PreflightCase,
) -> None:
    case = preflight_case
    with case.database.immediate_session() as session:
        session.execute(
            update(Workspace).where(Workspace.id == case.team_workspace_id).values(state="archived")
        )

    invalid_roots = (
        {
            "session_id": case.personal_session_id,
            "workspace_id": case.team_workspace_id,
        },
        {"session_id": "missing-session"},
        {"workspace_id": "missing-workspace"},
        {"workspace_id": case.team_workspace_id},
    )
    with patch(
        "pandrator.web.generation_provider_preflight.resolve_provider_credential",
        side_effect=AssertionError("invalid roots must fail before credential resolution"),
    ) as resolver:
        for roots in invalid_roots:
            with pytest.raises(ValueError):
                generation_provider_preflight(case.database, _vertex_snapshot(), **roots)

    resolver.assert_not_called()


def test_resolver_failure_returns_only_safe_workspace_diagnostics(
    preflight_case: PreflightCase,
) -> None:
    case = preflight_case
    with patch(
        "pandrator.web.generation_provider_preflight.resolve_provider_credential",
        side_effect=RuntimeError("/private/provider-token-path/sentinel"),
    ) as resolver:
        result = generation_provider_preflight(
            case.database, _vertex_snapshot(), session_id=case.team_session_id
        )

    resolver.assert_called_once_with(
        case.database,
        DataPaths(case.database.path.parent),
        "vertex_ai",
        "db:shared:vertex_ai",
        fallback_environment_variable="",
        workspace_id=case.team_workspace_id,
    )
    assert result["credential_origin"] == "unavailable"
    assert result["credential_configured"] is False
    assert result["project"] == ""
    assert result["project_resolution"] == "unavailable"
    assert result["configuration_warning"] == _WORKSPACE_WARNING
    assert "/private/provider-token-path/sentinel" not in repr(result)


def test_non_google_route_does_not_resolve_google_credentials(
    preflight_case: PreflightCase,
) -> None:
    case = preflight_case
    with patch(
        "pandrator.web.generation_provider_preflight.resolve_provider_credential",
        side_effect=AssertionError("non-Google routes must not resolve credentials"),
    ) as resolver:
        result = generation_provider_preflight(
            case.database,
            {"tts": {"service": "xtts", "model": "xtts-v2", "speaker": "neutral"}},
            session_id=case.team_session_id,
        )

    resolver.assert_not_called()
    assert result["route_id"] == "xtts"
    assert result["access_checked"] is False
    assert "credential_origin" not in result


def test_scoped_preflight_executes_no_database_writes(
    preflight_case: PreflightCase,
) -> None:
    case = preflight_case
    _store_credential(
        case,
        case.team_workspace_id,
        json.dumps({"type": "service_account", "project_id": "read-only-project"}),
    )
    with case.database.snapshot_session() as session:
        before = session.scalar(
            select(func.count())
            .select_from(StoredCredential)
            .where(StoredCredential.workspace_id == case.team_workspace_id)
        )

    statements: list[str] = []

    def capture_statement(
        _connection: object,
        _cursor: object,
        statement: str,
        _parameters: object,
        _context: object,
        _executemany: bool,
    ) -> None:
        statements.append(statement.lstrip().split(maxsplit=1)[0].upper())

    event.listen(case.database.engine, "before_cursor_execute", capture_statement)
    try:
        result = generation_provider_preflight(
            case.database, _vertex_snapshot(), session_id=case.team_session_id
        )
    finally:
        event.remove(case.database.engine, "before_cursor_execute", capture_statement)

    with case.database.snapshot_session() as session:
        after = session.scalar(
            select(func.count())
            .select_from(StoredCredential)
            .where(StoredCredential.workspace_id == case.team_workspace_id)
        )

    assert result["project"] == "read-only-project"
    assert before == after == 1
    assert statements
    assert not set(statements) & {
        "INSERT",
        "UPDATE",
        "DELETE",
        "REPLACE",
        "CREATE",
        "DROP",
        "ALTER",
    }
