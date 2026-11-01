"""Workspace-scoped provider configuration and model budgeting."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import select

from pandrator.logic import llm_handler
from pandrator.runtime import DataPaths
from pandrator.web.accounts.identity import initial_personal_workspace
from pandrator.web.accounts.models import Workspace
from pandrator.web.context_budget import (
    DEFAULT_CONTEXT_WINDOW_TOKENS,
    ContextBudgetService,
)
from pandrator.web.credentials import (
    ScopedCredentialValues,
    resolve_provider_credential,
    shared_provider_credential_key,
)
from pandrator.web.database import Database, sqlite_url
from pandrator.web.models import Provider, ProviderModel, StoredCredential, utcnow
from pandrator.web.provider_settings import build_llm_settings
from pandrator.web.sessions import SessionService
from tests.web_test_support import prepare_web_test_data_root


@dataclass(frozen=True)
class ProviderCase:
    database: Database
    paths: DataPaths
    workspaces: dict[str, str]
    sessions: dict[str, str]


@pytest.fixture
def provider_case(tmp_path: Path) -> Iterator[ProviderCase]:
    paths = prepare_web_test_data_root(tmp_path)
    database = Database(paths.database)
    workspaces: dict[str, str] = {}
    with database.immediate_session() as session:
        workspaces["personal"] = initial_personal_workspace(session).id
        for name in ("alpha", "beta"):
            workspace = Workspace(kind="team", name=name)
            session.add(workspace)
            session.flush()
            workspaces[name] = workspace.id

    sessions = {
        name: SessionService(database).create(name, workspace_id=workspaces[owner]).id
        for name, owner in (("alpha", "alpha"), ("beta", "beta"))
    }
    try:
        yield ProviderCase(database, paths, workspaces, sessions)
    finally:
        database.dispose()


def add_provider(
    case: ProviderCase,
    workspace_id: str | None,
    *,
    label: str,
    provider_key: str = "openai",
    enabled: bool = True,
    base_url: str | None = None,
    secret_ref: str | None = "db:provider-key",
    options: dict[str, Any] | None = None,
    models: tuple[dict[str, Any], ...] = (),
    credentials: dict[str, str] | None = None,
    created_at: Any = None,
) -> str:
    with case.database.immediate_session() as session:
        provider = Provider(
            kind="llm",
            provider_key=provider_key,
            label=label,
            enabled=enabled,
            base_url=base_url,
            secret_ref=secret_ref,
            options_json=dict(options or {}),
            workspace_id=workspace_id,
            **({"created_at": created_at} if created_at is not None else {}),
        )
        session.add(provider)
        session.flush()
        for model_values in models:
            session.add(ProviderModel(provider_id=provider.id, **model_values))
        for key, value in (credentials or {}).items():
            session.add(
                StoredCredential(
                    workspace_id=workspace_id,
                    key=key,
                    label=key,
                    secret_value=value,
                )
            )
        session.flush()
        return provider.id


def model(
    model_id: str,
    *,
    active: bool = True,
    default: bool = True,
    context_window: int = 32_000,
    max_output: int = 4_000,
    created_at: Any = None,
) -> dict[str, Any]:
    return {
        "model_id": model_id,
        "is_active": active,
        "is_default": default,
        "context_window_tokens": context_window,
        "max_output_tokens": max_output,
        **({"created_at": created_at} if created_at is not None else {}),
    }


class ForbiddenEnvironment(dict[str, str]):
    def get(self, key: str, default: Any = None) -> Any:
        raise AssertionError(f"unexpected environment lookup: {key}")

    def __getitem__(self, key: str) -> str:
        raise AssertionError(f"unexpected environment lookup: {key}")

    def __contains__(self, key: object) -> bool:
        raise AssertionError(f"unexpected environment lookup: {key}")


def test_same_provider_and_model_ids_keep_credentials_defaults_and_budgets_scoped(
    provider_case: ProviderCase,
) -> None:
    case = provider_case
    shared_key = shared_provider_credential_key("openai")
    alpha_id = add_provider(
        case,
        case.workspaces["alpha"],
        label="Shared binding",
        secret_ref="db:provider-key",
        models=(
            model("shared-model", default=False, context_window=20_000),
            model("alpha-default", context_window=30_000),
        ),
        credentials={
            "provider-key": "synthetic-direct-alpha",
            shared_key: "synthetic-shared-alpha",
        },
    )
    beta_id = add_provider(
        case,
        case.workspaces["beta"],
        label="Shared binding",
        secret_ref="db:provider-key",
        models=(
            model("shared-model", default=False, context_window=40_000),
            model("beta-default", context_window=50_000),
        ),
        credentials={
            "provider-key": "synthetic-direct-beta",
            shared_key: "synthetic-shared-beta",
        },
    )

    alpha, alpha_selector = build_llm_settings(
        case.database,
        case.paths,
        requested_model="shared-model",
        workspace_id=case.workspaces["alpha"],
    )
    beta, beta_selector = build_llm_settings(
        case.database,
        case.paths,
        requested_model="shared-model",
        workspace_id=case.workspaces["beta"],
    )
    assert alpha_selector == f"custom:{alpha_id}/shared-model"
    assert beta_selector == f"custom:{beta_id}/shared-model"
    assert alpha.provider_binding_id == alpha_id
    assert beta.provider_binding_id == beta_id
    assert alpha.workspace_id == case.workspaces["alpha"]
    assert beta.workspace_id == case.workspaces["beta"]
    assert alpha.provider_configs[0]["api_key"] == "synthetic-shared-alpha"
    assert beta.provider_configs[0]["api_key"] == "synthetic-shared-beta"
    assert alpha.provider_configs[0]["api_key_env"] == ""
    assert beta.provider_configs[0]["api_key_env"] == ""
    assert len(alpha.provider_configs) == len(beta.provider_configs) == 1
    assert "synthetic-shared-alpha" not in repr(alpha)
    assert "synthetic-shared-beta" not in repr(beta)

    alpha_default, alpha_default_selector = build_llm_settings(
        case.database, case.paths, workspace_id=case.workspaces["alpha"]
    )
    beta_default, beta_default_selector = build_llm_settings(
        case.database, case.paths, workspace_id=case.workspaces["beta"]
    )
    assert alpha_default_selector == f"custom:{alpha_id}/alpha-default"
    assert beta_default_selector == f"custom:{beta_id}/beta-default"
    assert alpha_default.default_model == alpha_default_selector
    assert beta_default.default_model == beta_default_selector

    budgets = ContextBudgetService(case.database)
    assert (
        budgets.resolve(alpha_selector, workspace_id=case.workspaces["alpha"]).context_window_tokens
        == 20_000
    )
    assert (
        budgets.resolve(beta_selector, workspace_id=case.workspaces["beta"]).context_window_tokens
        == 40_000
    )
    foreign_hint = budgets.resolve(
        f"custom:{beta_id}/shared-model", workspace_id=case.workspaces["alpha"]
    )
    assert foreign_hint.context_window_tokens == DEFAULT_CONTEXT_WINDOW_TOKENS


def test_legacy_personal_workspace_keeps_host_fallback_and_selector_aliases(
    provider_case: ProviderCase, monkeypatch: pytest.MonkeyPatch
) -> None:
    case = provider_case
    monkeypatch.setenv("SYNTHETIC_LEGACY_PROVIDER_KEY", "synthetic-ambient-key")
    add_provider(
        case,
        case.workspaces["personal"],
        label="Legacy custom endpoint",
        secret_ref="env:SYNTHETIC_LEGACY_PROVIDER_KEY",
        base_url="http://127.0.0.1:1234/v1",
        options={"is_custom": True, "provider_id": "legacy-alias"},
        models=(model("legacy-model", context_window=18_000),),
    )

    settings, selected = build_llm_settings(
        case.database, case.paths, requested_model="legacy-model"
    )
    assert selected == "custom:legacy-alias/legacy-model"
    assert settings.default_model == selected
    assert settings.workspace_id == case.workspaces["personal"]
    assert settings.provider_binding_id is None
    assert settings.provider_configs[0]["api_key_env"] == "SYNTHETIC_LEGACY_PROVIDER_KEY"
    assert ContextBudgetService(case.database).resolve(selected).context_window_tokens == 18_000


def test_personal_budget_does_not_replace_an_unmatched_provider_hint(
    provider_case: ProviderCase,
) -> None:
    case = provider_case
    add_provider(
        case,
        case.workspaces["personal"],
        label="Personal model",
        models=(model("shared-model", context_window=18_000),),
    )
    foreign_provider_id = add_provider(
        case,
        case.workspaces["beta"],
        label="Foreign model",
        models=(model("shared-model", context_window=48_000),),
    )
    budget = ContextBudgetService(case.database)
    for selector in (
        f"custom:{foreign_provider_id}/shared-model",
        "custom:missing-provider/shared-model",
        "missing-key/shared-model",
    ):
        assert budget.resolve(selector).context_window_tokens == DEFAULT_CONTEXT_WINDOW_TOKENS


def test_session_root_selects_workspace_and_rejects_workspace_mismatch(
    provider_case: ProviderCase, monkeypatch: pytest.MonkeyPatch
) -> None:
    case = provider_case
    provider_id = add_provider(
        case,
        case.workspaces["alpha"],
        label="Alpha session provider",
        models=(model("session-model"),),
        credentials={"provider-key": "synthetic-session-key"},
    )
    settings, selected = build_llm_settings(
        case.database,
        case.paths,
        session_id=case.sessions["alpha"],
        requested_model="session-model",
    )
    assert settings.workspace_id == case.workspaces["alpha"]
    assert selected == f"custom:{provider_id}/session-model"

    with patch(
        "pandrator.web.provider_settings.resolve_provider_credential",
        side_effect=AssertionError("credential lookup must follow ownership validation"),
    ):
        with pytest.raises(ValueError, match="workspace and project disagree"):
            build_llm_settings(
                case.database,
                case.paths,
                session_id=case.sessions["alpha"],
                workspace_id=case.workspaces["beta"],
                requested_model="session-model",
            )


def test_foreign_provider_and_custom_model_selectors_fail_before_credentials(
    provider_case: ProviderCase,
) -> None:
    case = provider_case
    add_provider(
        case,
        case.workspaces["alpha"],
        label="Alpha",
        models=(model("same-model"),),
        credentials={"provider-key": "synthetic-alpha"},
    )
    beta_id = add_provider(
        case,
        case.workspaces["beta"],
        label="Beta",
        enabled=False,
        models=(model("same-model"),),
        credentials={"provider-key": "synthetic-beta"},
    )
    with patch(
        "pandrator.web.provider_settings.resolve_provider_credential",
        side_effect=AssertionError("foreign selectors must fail before credential lookup"),
    ):
        with pytest.raises(ValueError, match="requested LLM provider is unavailable"):
            build_llm_settings(
                case.database,
                case.paths,
                requested_provider_id=beta_id,
                requested_model="same-model",
                workspace_id=case.workspaces["alpha"],
            )
        with pytest.raises(ValueError, match="unavailable or ambiguous"):
            build_llm_settings(
                case.database,
                case.paths,
                requested_model=f"custom:{beta_id}/same-model",
                workspace_id=case.workspaces["alpha"],
            )


def test_ambiguous_raw_selector_and_missing_default_fail_before_credentials(
    provider_case: ProviderCase,
) -> None:
    case = provider_case
    add_provider(
        case,
        case.workspaces["alpha"],
        label="First",
        models=(model("ambiguous-model"),),
    )
    add_provider(
        case,
        case.workspaces["alpha"],
        label="Second",
        models=(model("ambiguous-model"),),
    )
    no_default_id = add_provider(
        case,
        case.workspaces["alpha"],
        label="No default",
        secret_ref="db:no-default",
        models=(model("inactive", active=False, default=False),),
    )
    with patch(
        "pandrator.web.provider_settings.resolve_provider_credential",
        side_effect=AssertionError("selection must finish before credential lookup"),
    ):
        with pytest.raises(ValueError, match="unavailable or ambiguous"):
            build_llm_settings(
                case.database,
                case.paths,
                requested_model="ambiguous-model",
                workspace_id=case.workspaces["alpha"],
            )
        with pytest.raises(ValueError, match="select a default model"):
            build_llm_settings(
                case.database,
                case.paths,
                requested_provider_id=no_default_id,
                workspace_id=case.workspaces["alpha"],
            )
        with pytest.raises(ValueError, match="unavailable or ambiguous"):
            build_llm_settings(
                case.database,
                case.paths,
                requested_model="inactive",
                requested_provider_id=no_default_id,
                workspace_id=case.workspaces["alpha"],
            )


def test_disabled_provider_is_available_only_for_explicit_scoped_diagnostics(
    provider_case: ProviderCase,
) -> None:
    case = provider_case
    provider_id = add_provider(
        case,
        case.workspaces["alpha"],
        label="Disabled alpha",
        enabled=False,
        models=(model("diagnostic-model"),),
        credentials={"provider-key": "synthetic-disabled-key"},
    )
    with patch(
        "pandrator.web.provider_settings.resolve_provider_credential",
        side_effect=AssertionError("disabled providers are excluded from unscoped selection"),
    ):
        with pytest.raises(ValueError, match="select a default model"):
            build_llm_settings(case.database, case.paths, workspace_id=case.workspaces["alpha"])

    settings, selected = build_llm_settings(
        case.database,
        case.paths,
        requested_provider_id=provider_id,
        workspace_id=case.workspaces["alpha"],
    )
    assert selected == f"custom:{provider_id}/diagnostic-model"
    assert [config["id"] for config in settings.provider_configs] == [provider_id]


def test_null_and_inactive_workspace_rows_are_not_selected(
    provider_case: ProviderCase,
) -> None:
    case = provider_case
    with case.database.immediate_session() as session:
        workspace = session.get(Workspace, case.workspaces["beta"])
        assert workspace is not None
        workspace.state = "archived"
    with patch(
        "pandrator.web.provider_settings.resolve_provider_credential",
        side_effect=AssertionError("inactive workspace must fail before credential lookup"),
    ):
        with pytest.raises(ValueError, match="inactive workspace"):
            build_llm_settings(case.database, case.paths, workspace_id=case.workspaces["beta"])

    # Missing ownership is only representable before the mandatory-root migration.
    # Exercise current-schema archive behavior before entering that legacy fixture.
    config = Config()
    config.set_main_option("script_location", str(Path(__file__).parents[1] / "pandrator/web/migrations"))
    config.set_main_option("sqlalchemy.url", sqlite_url(case.database.path))
    command.downgrade(config, "0074_actor_attribution")
    null_provider_id = add_provider(
        case,
        None,
        label="Legacy null-owned",
        models=(model("null-model"),),
    )
    with patch(
        "pandrator.web.provider_settings.resolve_provider_credential",
        side_effect=AssertionError("null-owned provider must not reach credential lookup"),
    ):
        with pytest.raises(ValueError, match="requested LLM provider is unavailable"):
            build_llm_settings(
                case.database,
                case.paths,
                requested_provider_id=null_provider_id,
                requested_model="null-model",
                workspace_id=case.workspaces["alpha"],
            )


def test_noninitial_host_references_preloads_and_missing_database_values_are_denied(
    provider_case: ProviderCase, monkeypatch: pytest.MonkeyPatch
) -> None:
    case = provider_case
    alpha = case.workspaces["alpha"]
    beta = case.workspaces["beta"]
    host_provider = add_provider(
        case,
        alpha,
        label="Host referenced",
        secret_ref="env:SYNTHETIC_HOST_KEY",
        models=(model("host-model"),),
    )
    missing_provider = add_provider(
        case,
        alpha,
        label="Missing db key",
        secret_ref="db:missing-value",
        models=(model("missing-model"),),
    )
    with patch(
        "pandrator.web.provider_settings.resolve_provider_credential",
        side_effect=AssertionError("noninitial host reference must be rejected before lookup"),
    ):
        with pytest.raises(ValueError, match="Configure a database credential for this workspace"):
            build_llm_settings(
                case.database,
                case.paths,
                requested_provider_id=host_provider,
                workspace_id=alpha,
            )

    # Restore the process environment before pytest reports the result.
    with monkeypatch.context() as scoped:
        scoped.setattr("pandrator.web.credentials.os.environ", ForbiddenEnvironment())
        with pytest.raises(ValueError, match="Configure a database credential for this workspace"):
            build_llm_settings(
                case.database,
                case.paths,
                requested_provider_id=missing_provider,
                workspace_id=alpha,
            )
        with pytest.raises(ValueError, match="Credential preload does not belong"):
            resolve_provider_credential(
                case.database,
                case.paths,
                "openai",
                "db:other-key",
                preloaded_database_credentials=ScopedCredentialValues(
                    beta, {"other-key": "synthetic-wrong-scope"}
                ),
                workspace_id=alpha,
            )
        with pytest.raises(ValueError, match="requires a database credential"):
            resolve_provider_credential(
                case.database,
                case.paths,
                "openai",
                "env:SYNTHETIC_HOST_KEY",
                workspace_id=alpha,
            )


def test_actual_custom_selector_uses_database_key_without_environment_fallback(
    provider_case: ProviderCase, monkeypatch: pytest.MonkeyPatch
) -> None:
    case = provider_case
    provider_id = add_provider(
        case,
        case.workspaces["alpha"],
        label="Database OpenAI route",
        base_url=None,
        secret_ref="db:provider-key",
        models=(model("api-model"),),
        credentials={"provider-key": "synthetic-api-key"},
    )
    settings, selected = build_llm_settings(
        case.database,
        case.paths,
        requested_model="openai/api-model",
        workspace_id=case.workspaces["alpha"],
    )
    assert selected == f"custom:{provider_id}/api-model"
    assert settings.provider_configs[0]["id"] == provider_id
    assert settings.provider_configs[0]["is_custom"] is True
    assert settings.provider_configs[0]["api_key"] == "synthetic-api-key"
    assert settings.provider_configs[0]["api_key_env"] == ""
    assert settings.provider_configs[0]["api_base"] == "https://api.openai.com/v1"
    assert "synthetic-api-key" not in repr(settings)

    with monkeypatch.context() as scoped:
        scoped.setattr(llm_handler.os, "environ", ForbiddenEnvironment())
        details = llm_handler._resolve_model_request_details(selected, settings)
    assert details["is_custom"] is True
    assert details["provider_config"]["id"] == provider_id
    assert details["request_overrides"]["api_key"] == "synthetic-api-key"
    assert details["request_overrides"]["api_base"] == "https://api.openai.com/v1"


def test_default_inactive_model_resolves_again_by_its_canonical_selector(
    provider_case: ProviderCase,
) -> None:
    case = provider_case
    workspace_id = case.workspaces["alpha"]
    provider_id = add_provider(
        case,
        workspace_id,
        label="Default-only model",
        secret_ref="db:provider-key",
        models=(model("default-only", active=False, default=True),),
        credentials={"provider-key": "synthetic-provider-key"},
    )

    _initial_settings, selected = build_llm_settings(
        case.database,
        case.paths,
        workspace_id=workspace_id,
    )
    _resolved_settings, resolved_again = build_llm_settings(
        case.database,
        case.paths,
        requested_model=selected,
        workspace_id=workspace_id,
    )

    assert selected == f"custom:{provider_id}/default-only"
    assert resolved_again == selected


def test_noninitial_vertex_credentials_are_validated_and_transient(
    provider_case: ProviderCase,
) -> None:
    case = provider_case
    service_account = (
        '{"type":"service_account","project_id":"synthetic-project",'
        '"client_email":"synthetic@example.invalid",'
        '"private_key":"SYNTHETIC_PRIVATE_KEY",'
        '"token_uri":"https://oauth.example.invalid/token"}'
    )
    provider_id = add_provider(
        case,
        case.workspaces["alpha"],
        label="Vertex route",
        provider_key="vertex_ai",
        secret_ref="db:vertex-key",
        options={"request_options": {"vertex_location": "europe-west4"}},
        models=(model("vertex-model"),),
        credentials={"vertex-key": service_account},
    )
    settings, selected = build_llm_settings(
        case.database,
        case.paths,
        requested_provider_id=provider_id,
        workspace_id=case.workspaces["alpha"],
    )
    config = settings.provider_configs[0]
    assert selected == f"custom:{provider_id}/vertex-model"
    assert config["api_key"] == ""
    assert config["api_key_env"] == ""
    assert config["request_options"]["vertex_credentials"] == service_account
    assert config["request_options"]["vertex_project"] == "synthetic-project"
    assert config["request_options"]["vertex_location"] == "europe-west4"
    assert "SYNTHETIC_PRIVATE_KEY" not in repr(settings)
    with case.database.session() as session:
        stored_provider = session.get(Provider, provider_id)
        assert stored_provider is not None
        assert "SYNTHETIC_PRIVATE_KEY" not in repr(stored_provider.options_json)


def test_unsupported_provider_route_is_blocked_before_credential_lookup(
    provider_case: ProviderCase,
) -> None:
    case = provider_case
    provider_id = add_provider(
        case,
        case.workspaces["alpha"],
        label="Local unsupported route",
        provider_key="ollama",
        secret_ref="db:ollama-key",
        models=(model("local-model"),),
        credentials={"ollama-key": "synthetic-local-key"},
    )
    with patch(
        "pandrator.web.provider_settings.resolve_provider_credential",
        side_effect=AssertionError("unsupported routes must fail before credential lookup"),
    ):
        with pytest.raises(
            ValueError,
            match="This provider requires a host-approved binding before it can be used in this workspace.",
        ):
            build_llm_settings(
                case.database,
                case.paths,
                requested_provider_id=provider_id,
                workspace_id=case.workspaces["alpha"],
            )


def test_noninitial_openai_without_base_url_keeps_explicit_custom_failure(
    provider_case: ProviderCase,
) -> None:
    case = provider_case
    provider_id = add_provider(
        case,
        case.workspaces["alpha"],
        label="Explicit custom missing base",
        base_url=None,
        options={"is_custom": True},
        secret_ref="db:provider-key",
        models=(model("custom-model"),),
        credentials={"provider-key": "synthetic-custom-key"},
    )
    settings, selected = build_llm_settings(
        case.database,
        case.paths,
        requested_provider_id=provider_id,
        workspace_id=case.workspaces["alpha"],
    )
    assert selected == f"custom:{provider_id}/custom-model"
    assert settings.provider_configs[0]["api_base"] == ""
    with pytest.raises(ValueError, match="missing an API base URL"):
        llm_handler._resolve_model_request_details(selected, settings)


def test_noninitial_default_order_is_provider_then_model_creation_order(
    provider_case: ProviderCase,
) -> None:
    case = provider_case
    now = utcnow()
    first_tie_id = add_provider(
        case,
        case.workspaces["alpha"],
        label="First provider tie",
        secret_ref="db:first-tie-key",
        created_at=now,
        models=(
            model("first-tie-earlier", created_at=now),
            model("first-tie-later", created_at=now + timedelta(seconds=1)),
        ),
        credentials={"first-tie-key": "synthetic-first-tie"},
    )
    second_tie_id = add_provider(
        case,
        case.workspaces["alpha"],
        label="Second provider tie",
        secret_ref="db:second-tie-key",
        created_at=now,
        models=(model("second-tie", created_at=now - timedelta(seconds=1)),),
        credentials={"second-tie-key": "synthetic-second-tie"},
    )
    later_id = add_provider(
        case,
        case.workspaces["alpha"],
        label="Later provider",
        secret_ref="db:later-key",
        created_at=now + timedelta(seconds=1),
        models=(model("earlier-model", created_at=now - timedelta(seconds=2)),),
        credentials={"later-key": "synthetic-later"},
    )
    _settings, selected = build_llm_settings(
        case.database, case.paths, workspace_id=case.workspaces["alpha"]
    )
    with case.database.snapshot_session() as session:
        default_pairs = session.execute(
            select(Provider, ProviderModel)
            .join(ProviderModel, ProviderModel.provider_id == Provider.id)
            .where(
                Provider.workspace_id == case.workspaces["alpha"],
                ProviderModel.is_default.is_(True),
            )
        ).all()
    expected_provider, expected_model = min(
        default_pairs,
        key=lambda pair: (
            pair[0].created_at,
            pair[0].id,
            pair[1].created_at,
            pair[1].id,
        ),
    )
    assert selected == f"custom:{expected_provider.id}/{expected_model.model_id}"
    assert expected_provider.id in {first_tie_id, second_tie_id}
    assert selected != f"custom:{later_id}/earlier-model"
