"""Provider diagnostics must retain the selected database provider identity."""

from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest import mock

import pytest
from flask import Flask
from flask.testing import FlaskClient

from pandrator.logic.llm_handler import ChatCompletionResult, _resolve_model_request_details
from pandrator.web.api import create_app
from pandrator.web.auth import BootstrapTokenStore
from pandrator.web.provider_settings import build_llm_settings
from tests.web_test_support import prepare_web_test_data_root


@pytest.fixture
def application(tmp_path: Path) -> Iterator[tuple[Flask, FlaskClient, dict[str, str]]]:
    prepare_web_test_data_root(tmp_path)
    bootstrap = BootstrapTokenStore()
    token = bootstrap.issue()
    app = create_app(data_root=tmp_path, testing=True, bootstrap_tokens=bootstrap)
    client = app.test_client()
    csrf = client.post("/api/v1/auth/bootstrap", json={"token": token}).get_json()["csrf_token"]
    try:
        yield app, client, {"X-CSRF-Token": csrf}
    finally:
        app.extensions["pandrator"]["database"].dispose()


def add_provider(
    client: FlaskClient,
    headers: dict[str, str],
    label: str,
    *,
    custom: bool = True,
    enabled: bool = True,
    active: bool = True,
) -> dict[str, Any]:
    response = client.post(
        "/api/v1/providers",
        json={
            "provider_key": "openai",
            "label": label,
            "enabled": enabled,
            "base_url": f"https://{label.lower()}.invalid/v1",
            "api_key": f"fixture-key-{label}",
            "options": {"is_custom": custom},
        },
        headers=headers,
    )
    assert response.status_code == 201
    provider = response.get_json()
    assert isinstance(provider, dict)
    model = client.post(
        f"/api/v1/providers/{provider['id']}/models",
        json={"model_id": "shared-model", "is_active": active},
        headers=headers,
    )
    assert model.status_code == 201
    return provider


@pytest.mark.parametrize("custom", [True, False], ids=["custom", "builtin"])
@pytest.mark.parametrize("explicit_model", [True, False], ids=["explicit", "automatic"])
def test_provider_test_keeps_exact_target_when_model_ids_overlap(
    application: tuple[Flask, FlaskClient, dict[str, str]],
    custom: bool,
    explicit_model: bool,
) -> None:
    _app, client, headers = application
    first = add_provider(client, headers, "First", custom=custom)
    second = add_provider(client, headers, "Second", custom=custom)
    # Custom resolution used to choose the first match; builtin normalization
    # used to overwrite its settings with the last provider of the same kind.
    target = second if custom else first
    observations: list[dict[str, Any]] = []

    def completion(
        *, messages: list[dict[str, str]], model_name: str, llm_settings: SimpleNamespace
    ) -> ChatCompletionResult:
        assert messages == [{"role": "user", "content": "Reply with exactly OK."}]
        details = _resolve_model_request_details(model_name, llm_settings)
        observations.append({"details": details, "configs": llm_settings.provider_configs})
        return ChatCompletionResult(
            content="OK", model=str(details["model"]), cost=0.001, cost_source="fixture"
        )

    with mock.patch(
        "pandrator.logic.llm_handler.chat_completion_with_metadata", side_effect=completion
    ):
        response = client.post(
            f"/api/v1/providers/{target['id']}/test",
            json={"model_id": "shared-model"} if explicit_model else {},
            headers=headers,
        )
    assert response.status_code == 200
    assert len(observations) == 1
    details = observations[0]["details"]
    assert details["request_overrides"]["api_base"] == target["base_url"]
    assert details["provider_config"]["name"] == target["label"]
    assert [item["name"] for item in observations[0]["configs"]] == [target["label"]]
    if custom:
        assert details["request_overrides"]["api_key"] == f"fixture-key-{target['label']}"
    assert response.get_json() == {
        "ok": True,
        "model": "openai/shared-model",
        "response": "OK",
        "cost": 0.001,
        "cost_source": "fixture",
    }


def test_disabled_provider_can_be_tested_without_enabling_it(
    application: tuple[Flask, FlaskClient, dict[str, str]],
) -> None:
    _app, client, headers = application
    add_provider(client, headers, "First")
    target = add_provider(client, headers, "Second", enabled=False)
    observed: list[dict[str, Any]] = []

    def completion(
        *, model_name: str, llm_settings: SimpleNamespace, **_kwargs: Any
    ) -> ChatCompletionResult:
        observed.append(_resolve_model_request_details(model_name, llm_settings))
        return ChatCompletionResult(content="OK", model="openai/shared-model")

    with mock.patch(
        "pandrator.logic.llm_handler.chat_completion_with_metadata", side_effect=completion
    ):
        response = client.post(
            f"/api/v1/providers/{target['id']}/test",
            json={"model_id": "shared-model"},
            headers=headers,
        )
    assert response.status_code == 200
    assert observed[0]["request_overrides"]["api_base"] == target["base_url"]
    records = client.get("/api/v1/providers").get_json()["items"]
    assert next(item for item in records if item["id"] == target["id"])["enabled"] is False


@pytest.mark.parametrize(("model_id", "active"), [("unknown-model", True), ("shared-model", False)])
def test_provider_test_refuses_model_without_active_target_record(
    application: tuple[Flask, FlaskClient, dict[str, str]],
    model_id: str,
    active: bool,
) -> None:
    _app, client, headers = application
    add_provider(client, headers, "First")
    target = add_provider(client, headers, "Second", active=active)
    with mock.patch(
        "pandrator.logic.llm_handler.chat_completion_with_metadata",
        return_value=ChatCompletionResult(content="OK"),
    ) as completion:
        response = client.post(
            f"/api/v1/providers/{target['id']}/test", json={"model_id": model_id}, headers=headers
        )
    assert response.status_code == 422
    completion.assert_not_called()


def test_unknown_provider_is_not_replaced_by_application_default(
    application: tuple[Flask, FlaskClient, dict[str, str]],
) -> None:
    _app, client, headers = application
    add_provider(client, headers, "First")
    with mock.patch("pandrator.logic.llm_handler.chat_completion_with_metadata") as completion:
        response = client.post(
            "/api/v1/providers/missing/test", json={"model_id": "shared-model"}, headers=headers
        )
    assert response.status_code == 404
    completion.assert_not_called()


def test_unscoped_worker_resolution_retains_existing_configuration_order(
    application: tuple[Flask, FlaskClient, dict[str, str]],
) -> None:
    app, client, headers = application
    first = add_provider(client, headers, "First")
    add_provider(client, headers, "Second")
    services = app.extensions["pandrator"]
    settings, selected = build_llm_settings(
        services["database"],
        services["paths"],
        requested_model="shared-model",
        request_timeout_seconds=777,
    )
    assert selected == f"custom:{first['id']}/shared-model"
    assert [item["name"] for item in settings.provider_configs] == ["First", "Second"]
    assert settings.request_timeout_seconds == 777
