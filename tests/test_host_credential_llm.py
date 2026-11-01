"""LLM helper qualification; direct SQL role setup is not account-command acceptance."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from unittest.mock import Mock

import pytest
from sqlalchemy import select, update

from pandrator.logic import llm_handler
from pandrator.web import provider_settings
from pandrator.web.accounts.models import User
from pandrator.web.credential_identity import credential_namespace
from pandrator.web.models import Provider, ProviderModel, utcnow
from pandrator.web.provider_settings import build_llm_settings
from tests.test_provider_workspace_resolution import (
    ForbiddenEnvironment,
    ProviderCase,
    add_provider,
    model,
)
from tests.test_provider_workspace_resolution import (
    provider_case as provider_case,
)


def _retire_original_owner(case: ProviderCase, state: str) -> None:
    # Disablement independently retains admin=True; neither role condition may
    # substitute for the other. This bypass is only helper fixture setup.
    with case.database.immediate_session() as session:
        session.execute(
            update(User)
            .where(User.legacy_subject == "owner")
            .values(is_active=state != "disabled", is_instance_admin=state != "demoted")
        )


@contextmanager
def _forbidden_host(monkeypatch: pytest.MonkeyPatch) -> Iterator[Mock]:
    getenv = Mock(side_effect=AssertionError("LLM helper attempted a host getenv"))
    sdk = Mock(side_effect=AssertionError("LLM helper attempted SDK initialization"))
    with monkeypatch.context() as scoped:
        scoped.setattr(
            llm_handler.os,
            "environ",
            ForbiddenEnvironment({"OPENAI_API_KEY": "synthetic-hostile-host-key"}),
        )
        scoped.setattr(llm_handler.os, "getenv", getenv)
        scoped.setattr(llm_handler, "_get_litellm_clients", sdk)
        yield getenv
    getenv.assert_not_called()
    sdk.assert_not_called()


@pytest.mark.parametrize("state", ("demoted", "disabled"))
def test_retired_original_openai_binding_reaches_native_helpers_with_database_key(
    provider_case: ProviderCase, monkeypatch: pytest.MonkeyPatch, state: str
) -> None:
    case = provider_case
    workspace_id = case.workspaces["personal"]
    provider_id = add_provider(
        case,
        workspace_id,
        label="Existing original OpenAI",
        models=(model("synthetic-model"),),
        credentials={"provider-key": "synthetic-original-database-key"},
    )
    active, active_selector = build_llm_settings(
        case.database, case.paths, workspace_id=workspace_id
    )
    assert active_selector == "openai/synthetic-model"
    assert active.provider_binding_id is None
    _retire_original_owner(case, state)
    with case.database.snapshot_session() as session:
        namespace = credential_namespace(session, workspace_id)
    assert namespace.legacy_host_access and not namespace.host_credentials_allowed
    api_key_resolver = Mock(wraps=llm_handler._resolve_api_key)
    with _forbidden_host(monkeypatch):
        with monkeypatch.context() as scoped:
            scoped.setattr(llm_handler, "_resolve_api_key", api_key_resolver)
            settings, selected = build_llm_settings(
                case.database,
                case.paths,
                requested_model="openai/synthetic-model",
                workspace_id=workspace_id,
            )
            details = llm_handler._resolve_model_request_details(selected, settings)
            status = llm_handler.validate_model_credentials(selected, settings)
    assert selected == f"custom:{provider_id}/synthetic-model"
    assert settings.provider_binding_id == provider_id
    assert details["request_overrides"]["api_key"] == "synthetic-original-database-key"
    assert details["request_overrides"]["api_base"] == "https://api.openai.com/v1"
    assert status.ok and status.provider_id == provider_id
    assert len(api_key_resolver.call_args_list) == 2
    assert all(
        call.args[0]["api_key"] == "synthetic-original-database-key"
        and call.args[0]["api_key_env"] == ""
        for call in api_key_resolver.call_args_list
    )
    assert "synthetic-original-database-key" not in repr(settings)


@pytest.mark.parametrize("state", ("demoted", "disabled"))
@pytest.mark.parametrize(
    ("secret_ref", "requested_model", "provider_key", "message"),
    (
        ("db:missing", "synthetic-model", "openai", "Configure a database credential"),
        ("", "synthetic-model", "openai", "Configure a database credential"),
        ("env:OPENAI_API_KEY", "synthetic-model", "openai", "Configure a database credential"),
        (
            "keyring:Pandrator/synthetic",
            "synthetic-model",
            "openai",
            "Configure a database credential",
        ),
        (
            "file-path:/synthetic/never-read",
            "synthetic-model",
            "openai",
            "Configure a database credential",
        ),
        ("db:provider-key", "unconfigured-model", "openai", "unavailable or ambiguous"),
        ("db:provider-key", "synthetic-model", "bedrock", "host-approved binding"),
    ),
)
def test_retired_original_invalid_binding_fails_before_native_helpers(
    provider_case: ProviderCase,
    monkeypatch: pytest.MonkeyPatch,
    state: str,
    secret_ref: str,
    requested_model: str,
    provider_key: str,
    message: str,
) -> None:
    case = provider_case
    provider_id = add_provider(
        case,
        case.workspaces["personal"],
        label="Invalid original binding",
        provider_key=provider_key,
        secret_ref=secret_ref,
        models=(model("synthetic-model"),),
        credentials={"provider-key": "synthetic-unused-key"},
    )
    _retire_original_owner(case, state)
    native = Mock(side_effect=AssertionError("Native helper must follow builder admission"))
    with _forbidden_host(monkeypatch):
        with monkeypatch.context() as scoped:
            scoped.setattr(llm_handler, "_resolve_model_request_details", native)
            scoped.setattr(llm_handler, "validate_model_credentials", native)
            with pytest.raises(ValueError, match=message):
                build_llm_settings(
                    case.database,
                    case.paths,
                    requested_provider_id=provider_id,
                    requested_model=requested_model,
                    workspace_id=case.workspaces["personal"],
                )
    native.assert_not_called()


@pytest.mark.parametrize("state", ("demoted", "disabled"))
@pytest.mark.parametrize(
    "selector", ("raw", "builtin", "canonical", "custom-alias", "at-alias", "slash-alias")
)
def test_retired_original_preserves_unique_legacy_custom_selector_aliases(
    provider_case: ProviderCase, monkeypatch: pytest.MonkeyPatch, state: str, selector: str
) -> None:
    case = provider_case
    provider_id = add_provider(
        case,
        case.workspaces["personal"],
        label="Legacy custom alias",
        base_url="https://synthetic-endpoint.invalid/v1",
        options={"is_custom": True, "provider_id": "legacy-alias"},
        models=(model("legacy-model"),),
        credentials={"provider-key": "synthetic-legacy-db-key"},
    )
    selectors = {
        "raw": "legacy-model",
        "builtin": "openai/legacy-model",
        "canonical": f"custom:{provider_id}/legacy-model",
        "custom-alias": "custom:legacy-alias/legacy-model",
        "at-alias": "legacy-model@legacy-alias",
        "slash-alias": "legacy-alias/legacy-model",
    }
    _retire_original_owner(case, state)
    with _forbidden_host(monkeypatch):
        settings, selected = build_llm_settings(
            case.database,
            case.paths,
            requested_model=selectors[selector],
            workspace_id=case.workspaces["personal"],
        )
    assert selected == f"custom:{provider_id}/legacy-model"
    assert settings.provider_binding_id == provider_id
    assert settings.provider_configs[0]["api_key"] == "synthetic-legacy-db-key"


@pytest.mark.parametrize(
    "selector",
    ("custom:shared-alias/shared-model", "shared-model@shared-alias", "shared-alias/shared-model"),
)
def test_legacy_alias_collision_is_rejected_before_credential_selection(
    provider_case: ProviderCase, monkeypatch: pytest.MonkeyPatch, selector: str
) -> None:
    case = provider_case
    for number in range(2):
        add_provider(
            case,
            case.workspaces["personal"],
            label=f"Ambiguous binding {number}",
            options={"is_custom": True, "provider_id": "shared-alias"},
            models=(model("shared-model"),),
            secret_ref=f"db:binding-{number}",
            credentials={f"binding-{number}": f"synthetic-binding-{number}"},
        )
    _retire_original_owner(case, "demoted")
    resolve = Mock(side_effect=AssertionError("Ambiguous alias selected credentials"))
    monkeypatch.setattr(provider_settings, "resolve_provider_credential", resolve)
    with pytest.raises(ValueError, match="unavailable or ambiguous"):
        build_llm_settings(
            case.database,
            case.paths,
            requested_model=selector,
            workspace_id=case.workspaces["personal"],
        )
    resolve.assert_not_called()


@pytest.mark.parametrize("workspace", ("personal", "alpha"))
def test_alias_options_do_not_expand_noncustom_or_noninitial_selectors(
    provider_case: ProviderCase, workspace: str
) -> None:
    case = provider_case
    add_provider(
        case,
        case.workspaces[workspace],
        label="Alias outside original custom shape",
        options={"provider_id": "ignored-alias", "is_custom": workspace == "alpha"},
        models=(model("synthetic-model"),),
        credentials={"provider-key": "synthetic-db-key"},
    )
    _retire_original_owner(case, "demoted")
    with pytest.raises(ValueError, match="unavailable or ambiguous"):
        build_llm_settings(
            case.database,
            case.paths,
            requested_model="custom:ignored-alias/synthetic-model",
            workspace_id=case.workspaces[workspace],
        )


def test_legacy_custom_shape_includes_nonbuiltin_provider_keys(provider_case: ProviderCase) -> None:
    case = provider_case
    provider_id = add_provider(
        case,
        case.workspaces["personal"],
        label="Legacy OpenRouter alias",
        provider_key="openrouter",
        options={"provider_id": "router-alias"},
        models=(model("synthetic-model"),),
        credentials={"provider-key": "synthetic-router-key"},
    )
    _retire_original_owner(case, "demoted")
    _settings, selected = build_llm_settings(
        case.database,
        case.paths,
        requested_model="synthetic-model@router-alias",
        workspace_id=case.workspaces["personal"],
    )
    assert selected == f"custom:{provider_id}/synthetic-model"


def test_legacy_default_selection_preserves_input_order_at_creation_time_ties(
    provider_case: ProviderCase,
) -> None:
    case = provider_case
    created_at = utcnow()
    for number in range(2):
        add_provider(
            case,
            case.workspaces["personal"],
            label=f"Default binding {number}",
            models=(model(f"model-{number}", created_at=created_at),),
            created_at=created_at,
        )
    with case.database.snapshot_session() as session:
        providers = list(
            session.scalars(
                select(Provider).where(Provider.workspace_id == case.workspaces["personal"])
            )
        )
        rows = list(
            session.scalars(
                select(ProviderModel).where(
                    ProviderModel.provider_id.in_([p.id for p in providers])
                )
            )
        )
    # Deliberately ordered loaded rows distinguish source order from stable-ID
    # tie sorting without depending on a SQLite query planner's choice of ties.
    providers.sort(key=lambda provider: provider.id, reverse=True)
    legacy_provider, legacy_row = provider_settings._select_scoped_provider_model(
        providers, rows, "", legacy_compatibility=True
    )
    scoped_provider, _scoped_row = provider_settings._select_scoped_provider_model(
        providers, rows, ""
    )
    assert legacy_provider.id == providers[0].id
    assert legacy_row.provider_id == legacy_provider.id
    assert scoped_provider.id == providers[-1].id


@pytest.mark.parametrize("state", ("demoted", "disabled"))
def test_other_workspace_database_execution_is_independent_of_original_retirement(
    provider_case: ProviderCase, monkeypatch: pytest.MonkeyPatch, state: str
) -> None:
    case = provider_case
    provider_id = add_provider(
        case,
        case.workspaces["alpha"],
        label="Independent workspace",
        models=(model("synthetic-model"),),
        credentials={"provider-key": "synthetic-independent-key"},
    )
    _retire_original_owner(case, state)
    with _forbidden_host(monkeypatch):
        settings, selected = build_llm_settings(
            case.database, case.paths, workspace_id=case.workspaces["alpha"]
        )
        details = llm_handler._resolve_model_request_details(selected, settings)
        assert llm_handler.validate_model_credentials(selected, settings).ok
    assert selected == f"custom:{provider_id}/synthetic-model"
    assert details["request_overrides"]["api_key"] == "synthetic-independent-key"


@pytest.mark.parametrize("state", ("demoted", "disabled"))
def test_retired_original_vertex_uses_validated_explicit_database_json(
    provider_case: ProviderCase, monkeypatch: pytest.MonkeyPatch, state: str
) -> None:
    case = provider_case
    service_account = (
        '{"type":"service_account","project_id":"synthetic-project",'
        '"client_email":"synthetic@example.invalid","private_key":"SYNTHETIC_PRIVATE_KEY",'
        '"token_uri":"https://oauth.example.invalid/token"}'
    )
    provider_id = add_provider(
        case,
        case.workspaces["personal"],
        label="Original Vertex binding",
        provider_key="vertex_ai",
        secret_ref="db:vertex-key",
        options={"request_options": {"vertex_location": "europe-west4"}},
        models=(model("vertex-model"),),
        credentials={"vertex-key": service_account},
    )
    _retire_original_owner(case, state)
    with _forbidden_host(monkeypatch):
        settings, selected = build_llm_settings(
            case.database, case.paths, workspace_id=case.workspaces["personal"]
        )
        details = llm_handler._resolve_model_request_details(selected, settings)
        assert llm_handler.validate_model_credentials(selected, settings).ok
    assert selected == f"custom:{provider_id}/vertex-model"
    assert settings.provider_configs[0]["api_key_env"] == ""
    assert details["request_overrides"]["vertex_credentials"] == service_account
    assert details["request_overrides"]["vertex_project"] == "synthetic-project"
    assert details["request_overrides"]["vertex_location"] == "europe-west4"
    assert "SYNTHETIC_PRIVATE_KEY" not in repr(settings)
    with case.database.snapshot_session() as session:
        assert (
            "vertex_credentials"
            not in session.get_one(Provider, provider_id).options_json["request_options"]
        )


def test_invalid_vertex_database_json_fails_before_native_helpers(
    provider_case: ProviderCase, monkeypatch: pytest.MonkeyPatch
) -> None:
    case = provider_case
    add_provider(
        case,
        case.workspaces["personal"],
        label="Invalid Vertex binding",
        provider_key="vertex_ai",
        models=(model("vertex-model"),),
        credentials={"provider-key": "synthetic-invalid-json"},
    )
    _retire_original_owner(case, "demoted")
    native = Mock(side_effect=AssertionError("Invalid Vertex reached native helper"))
    monkeypatch.setattr(llm_handler, "_resolve_model_request_details", native)
    with _forbidden_host(monkeypatch):
        with pytest.raises(ValueError, match="JSON"):
            build_llm_settings(case.database, case.paths, workspace_id=case.workspaces["personal"])
    native.assert_not_called()


@pytest.mark.parametrize("custom", (False, True))
def test_active_original_admin_retains_legacy_selectors_and_environment_fallback(
    provider_case: ProviderCase, monkeypatch: pytest.MonkeyPatch, custom: bool
) -> None:
    case = provider_case
    add_provider(
        case,
        case.workspaces["personal"],
        label="Active original control",
        base_url="https://synthetic-endpoint.invalid/v1" if custom else None,
        secret_ref="env:OPENAI_API_KEY",
        options={"is_custom": custom, "provider_id": "legacy-alias"} if custom else {},
        models=(model("synthetic-model"),),
    )
    with monkeypatch.context() as scoped:
        scoped.setattr(llm_handler.os, "environ", {"OPENAI_API_KEY": "synthetic-host-control"})
        settings, selected = build_llm_settings(
            case.database,
            case.paths,
            requested_model="synthetic-model",
            workspace_id=case.workspaces["personal"],
        )
        details = llm_handler._resolve_model_request_details(selected, settings)
    assert selected == (
        "custom:legacy-alias/synthetic-model" if custom else "openai/synthetic-model"
    )
    assert settings.provider_binding_id is None
    assert details["request_overrides"]["api_key"] == "synthetic-host-control"
