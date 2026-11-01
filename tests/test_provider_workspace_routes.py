"""Solo routes and backend metadata readers respect private provider ownership."""

from __future__ import annotations

import os
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock, patch

import pytest
from alembic import command
from alembic.config import Config
from flask.testing import FlaskClient
from sqlalchemy import select, update

from pandrator.logic import llm_handler
from pandrator.logic.llm_handler import ModelDiscoveryResult
from pandrator.runtime import DataPaths
from pandrator.web import cli
from pandrator.web.accounts.identity import initial_personal_workspace
from pandrator.web.api import create_app
from pandrator.web.auth import BootstrapTokenStore
from pandrator.web.credentials import (
    database_reference,
    shared_provider_credential_key,
    upsert_credential,
)
from pandrator.web.database import Database, sqlite_url
from pandrator.web.dispatch import DispatchRunService
from pandrator.web.models import Provider, ProviderModel, SessionRecord, StoredCredential, Workspace
from pandrator.web.provider_identity import (
    model_in_namespace,
    provider_in_namespace,
    provider_namespace,
)
from pandrator.web.sessions import SessionService
from pandrator.web.subtitle_evidence import SubtitleEvidenceService
from pandrator.web.workflow_plans import WorkflowExecutionPlanService
from pandrator.web.workflow_prerequisites import WorkflowPrerequisiteService
from tests.web_test_support import prepare_web_test_data_root


@dataclass
class ProviderCase:
    database: Database
    paths: DataPaths
    client: FlaskClient
    headers: dict[str, str]
    workspaces: dict[str, str]
    sessions: dict[str, SessionRecord]

    def create(
        self,
        owner: str,
        *,
        label: str = "Synthetic provider",
        model: str = "fixture-model",
        default: bool = False,
        secret_ref: str | None = None,
    ) -> tuple[str, str]:
        with self.database.immediate_session() as session:
            provider = Provider(
                workspace_id=self.workspaces[owner],
                kind="llm",
                provider_key="openai",
                label=label,
                secret_ref=secret_ref,
            )
            session.add(provider)
            session.flush()
            row = ProviderModel(
                provider_id=provider.id,
                model_id=model,
                is_active=True,
                is_default=default,
                input_modalities_json=["text", "audio"],
            )
            session.add(row)
            session.flush()
            return provider.id, row.id


@pytest.fixture
def provider_case(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[ProviderCase]:
    monkeypatch.setattr(os, "environ", {})
    paths = prepare_web_test_data_root(tmp_path)
    bootstrap = BootstrapTokenStore()
    token = bootstrap.issue()
    app = create_app(
        data_root=paths.root, testing=True, bootstrap_tokens=bootstrap, background_maintenance=False
    )
    client = app.test_client()
    response = client.post("/api/v1/auth/bootstrap", json={"token": token})
    headers = {"X-CSRF-Token": response.get_json()["csrf_token"]}
    database: Database = app.extensions["pandrator"]["database"]
    with database.immediate_session() as session:
        workspaces = {"personal": initial_personal_workspace(session).id}
        for name in ("alpha", "beta"):
            workspace = Workspace(kind="team", name=name)
            session.add(workspace)
            session.flush()
            workspaces[name] = workspace.id
    service = SessionService(database)
    sessions = {
        name: service.create(name, workspace_id=identity) for name, identity in workspaces.items()
    }
    try:
        yield ProviderCase(database, paths, client, headers, workspaces, sessions)
    finally:
        database.dispose()


def test_solo_provider_routes_hide_foreign_and_null_owned_records_before_projection(
    provider_case: ProviderCase,
) -> None:
    case = provider_case
    own, own_model = case.create("personal")
    foreign, foreign_model = case.create("alpha")
    # This defensive read fixture represents the real nullable predecessor;
    # current schema rejects unowned rows before a route can observe them.
    config = Config()
    config.set_main_option("script_location", str(Path(__file__).parents[1] / "pandrator/web/migrations"))
    config.set_main_option("sqlalchemy.url", sqlite_url(case.database.path))
    command.downgrade(config, "0074_actor_attribution")
    with case.database.immediate_session() as session:
        unowned = Provider(kind="llm", provider_key="openai", label="Unowned")
        session.add(unowned)
        session.flush()
        unowned_id = unowned.id
    response = case.client.get("/api/v1/providers")
    assert [item["id"] for item in response.get_json()["items"]] == [own]
    for provider_id in (foreign, unowned_id, "missing-provider"):
        assert case.client.get(f"/api/v1/providers/{provider_id}/models").status_code == 404
        assert (
            case.client.patch(
                f"/api/v1/providers/{provider_id}",
                json={"label": "Changed"},
                headers={**case.headers, "If-Match": '"1"'},
            ).status_code
            == 404
        )
        assert (
            case.client.delete(
                f"/api/v1/providers/{provider_id}", json={}, headers=case.headers
            ).status_code
            == 404
        )
        assert (
            case.client.post(
                f"/api/v1/providers/{provider_id}/models",
                json={"model_id": "other"},
                headers=case.headers,
            ).status_code
            == 404
        )
        with (
            patch("pandrator.logic.llm_handler.chat_completion_with_metadata") as completion,
            patch("pandrator.logic.llm_handler.discover_provider_models") as discover,
        ):
            assert (
                case.client.post(
                    f"/api/v1/providers/{provider_id}/test", json={}, headers=case.headers
                ).status_code
                == 404
            )
            assert (
                case.client.post(
                    f"/api/v1/providers/{provider_id}/models/refresh", json={}, headers=case.headers
                ).status_code
                == 404
            )
        completion.assert_not_called()
        discover.assert_not_called()
    assert (
        case.client.patch(
            f"/api/v1/providers/{own}/models/{foreign_model}",
            json={"is_active": False},
            headers={**case.headers, "If-Match": '"1"'},
        ).status_code
        == 404
    )
    assert (
        case.client.delete(
            f"/api/v1/providers/{own}/models/{foreign_model}", json={}, headers=case.headers
        ).status_code
        == 404
    )
    assert (
        case.client.get(f"/api/v1/providers/{own}/models").get_json()["items"][0]["id"] == own_model
    )


def test_route_creation_assigns_the_solo_owner_and_never_replaces_team_credentials(
    provider_case: ProviderCase,
) -> None:
    case = provider_case
    case.create("alpha", label="Same identity")
    shared = shared_provider_credential_key("openai")
    with case.database.immediate_session() as session:
        upsert_credential(
            session,
            shared,
            "Synthetic team fixture",
            "synthetic-team-value",
            workspace_id=case.workspaces["alpha"],
        )
    response = case.client.post(
        "/api/v1/providers",
        json={
            "provider_key": "openai",
            "label": "Same identity",
            "api_key": "synthetic-solo-value",
        },
        headers=case.headers,
    )
    assert response.status_code == 201
    assert "synthetic-solo-value" not in response.get_data(as_text=True)
    with case.database.snapshot_session() as session:
        provider = session.get_one(Provider, response.get_json()["id"])
        assert provider.workspace_id == case.workspaces["personal"]
        assert provider.secret_ref == database_reference(shared)
        assert (
            session.get_one(StoredCredential, (case.workspaces["alpha"], shared)).secret_value
            == "synthetic-team-value"
        )
        assert (
            session.get_one(StoredCredential, (case.workspaces["personal"], shared)).secret_value
            == "synthetic-solo-value"
        )


def test_model_default_promotion_and_replacement_cannot_cross_workspaces(
    provider_case: ProviderCase,
) -> None:
    case = provider_case
    own, own_model = case.create("personal", default=True)
    foreign, foreign_model = case.create("alpha", default=True)
    created = case.client.post(
        f"/api/v1/providers/{own}/models",
        json={"model_id": "second-model", "is_default": True},
        headers=case.headers,
    )
    assert created.status_code == 201
    replacement_id = created.get_json()["id"]
    with case.database.snapshot_session() as session:
        assert not session.get_one(ProviderModel, own_model).is_default
        assert session.get_one(ProviderModel, foreign_model).is_default
    changed = case.client.patch(
        f"/api/v1/providers/{own}/models/{own_model}",
        json={"is_default": True},
        headers={**case.headers, "If-Match": '"1"'},
    )
    assert changed.status_code == 200
    for endpoint, body in (
        (f"/api/v1/providers/{own}", {"replacement_model_record_id": foreign_model}),
        (
            f"/api/v1/providers/{own}/models/{own_model}",
            {"replacement_model_record_id": foreign_model},
        ),
    ):
        assert case.client.delete(endpoint, json=body, headers=case.headers).status_code == 409
    with case.database.snapshot_session() as session:
        assert session.get_one(ProviderModel, own_model).is_default
        assert not session.get_one(ProviderModel, replacement_id).is_default
        assert session.get_one(ProviderModel, foreign_model).is_default
        assert session.get_one(Provider, foreign).workspace_id == case.workspaces["alpha"]


def test_failed_provider_cleanup_rolls_back_default_replacement(
    provider_case: ProviderCase,
) -> None:
    case = provider_case
    own, own_model = case.create("personal", default=True, secret_ref="keyring:synthetic/fixture")
    _, replacement_id = case.create("personal", label="Replacement")
    with patch(
        "pandrator.web.credentials._delete_keyring_reference",
        side_effect=RuntimeError("Synthetic cleanup failure"),
    ):
        response = case.client.delete(
            f"/api/v1/providers/{own}",
            json={"replacement_model_record_id": replacement_id},
            headers=case.headers,
        )
    assert response.status_code == 422
    with case.database.snapshot_session() as session:
        assert session.get(Provider, own) is not None
        assert session.get_one(ProviderModel, own_model).is_default
        assert not session.get_one(ProviderModel, replacement_id).is_default


@pytest.mark.parametrize("change", ("provider_revision", "concurrent_model"))
def test_discovery_releases_its_reader_and_revalidates_before_publication(
    provider_case: ProviderCase,
    change: str,
) -> None:
    case = provider_case
    own, _ = case.create("personal")

    def discover(_configuration: dict[str, Any]) -> ModelDiscoveryResult:
        # A writer must be available while discovery runs, including in WAL mode.
        with case.database.immediate_session(busy_timeout_ms=50) as session:
            if change == "provider_revision":
                session.execute(update(Provider).where(Provider.id == own).values(revision=2))
            else:
                session.add(ProviderModel(provider_id=own, model_id="racing-model"))
        return ModelDiscoveryResult(models=("racing-model", "new-model"), source="api")

    with patch("pandrator.logic.llm_handler.discover_provider_models", side_effect=discover):
        response = case.client.post(
            f"/api/v1/providers/{own}/models/refresh", json={}, headers=case.headers
        )
    assert response.status_code == (409 if change == "provider_revision" else 200)
    with case.database.snapshot_session() as session:
        models = session.scalars(
            select(ProviderModel).where(ProviderModel.provider_id == own)
        ).all()
        if change == "provider_revision":
            assert [row.model_id for row in models] == ["fixture-model"]
        else:
            assert sorted(row.model_id for row in models) == [
                "fixture-model",
                "new-model",
                "racing-model",
            ]
            assert all(not row.is_default for row in models)


def test_metadata_helpers_filter_before_labels_snapshots_and_default_flags(
    provider_case: ProviderCase,
) -> None:
    case = provider_case
    own, own_model = case.create("alpha", default=True)
    _, foreign_model = case.create("beta", default=True)
    sid = case.sessions["alpha"].id
    with case.database.snapshot_session() as session:
        namespace = provider_namespace(session, session_id=sid)
        assert provider_in_namespace(session, own, namespace) is not None
        assert model_in_namespace(session, foreign_model, namespace) is None
        witness = DispatchRunService._audio_witness_models(session, session_id=sid)
        assert [row["id"] for row in witness] == [own_model]
        SubtitleEvidenceService._validate_audio_models(session, [own_model], session_id=sid)
        with pytest.raises(ValueError, match="was not found"):
            SubtitleEvidenceService._validate_audio_models(session, [foreign_model], session_id=sid)
        state = WorkflowExecutionPlanService._state_snapshot(session, sid)
        assert [row["id"] for row in state["providers"]] == [own]
        assert [row["id"] for row in state["provider_models"]] == [own_model]


def test_cli_provider_list_preserves_the_default_private_namespace(
    provider_case: ProviderCase,
) -> None:
    case = provider_case
    own, _ = case.create("personal")
    case.create("alpha")
    output = Mock()
    with (
        patch.object(cli, "_database", return_value=(case.paths, case.database)),
        patch.object(cli, "_emit", output),
    ):
        assert cli.command_provider_list(SimpleNamespace(json=True)) == 0
    assert [row["id"] for row in output.call_args.args[0]] == [own]


def test_worker_hydrator_keeps_the_actual_provider_binding_in_the_execution_selector(
    provider_case: ProviderCase,
) -> None:
    case = provider_case
    key = shared_provider_credential_key("openai")
    provider_id, _ = case.create("alpha", default=True, secret_ref=database_reference(key))
    with case.database.immediate_session() as session:
        upsert_credential(
            session,
            key,
            "Synthetic scoped worker",
            "synthetic-worker-value",
            workspace_id=case.workspaces["alpha"],
        )
    service = WorkflowPrerequisiteService(case.database, case.paths)
    with patch.object(
        llm_handler.os, "getenv", side_effect=AssertionError("Host fallback must not run")
    ):
        hydrated = service._with_database_llm_settings(
            {"model_name": "openai/fixture-model"},
            "translation",
            session_id=case.sessions["alpha"].id,
        )
        runtime = SimpleNamespace(
            provider_configs=hydrated["llm_provider_configs"],
            default_model=hydrated["llm_default_model"],
        )
        assert hydrated["translation_model"] == f"custom:{provider_id}/fixture-model"
        details = llm_handler._resolve_model_request_details(hydrated["translation_model"], runtime)
    assert details["request_overrides"]["api_key"] == "synthetic-worker-value"
