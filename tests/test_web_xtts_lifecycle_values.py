"""Malformed wrapper timestamps must not prevent listing usable XTTS models."""

from collections.abc import Iterator
from unittest import mock

import pytest
import requests
from flask import Flask
from flask.testing import FlaskClient

from pandrator.web.api import create_app
from pandrator.web.auth import BootstrapTokenStore
from tests.web_test_support import prepare_web_test_data_root


@pytest.fixture(scope="module")
def application(
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[tuple[Flask, FlaskClient, dict[str, str]]]:
    root = tmp_path_factory.mktemp("xtts-lifecycle-values")
    prepare_web_test_data_root(root)
    bootstrap = BootstrapTokenStore()
    token = bootstrap.issue()
    app = create_app(data_root=root, testing=True, bootstrap_tokens=bootstrap)
    client = app.test_client()
    csrf = client.post("/api/v1/auth/bootstrap", json={"token": token}).get_json()["csrf_token"]
    try:
        yield app, client, {"X-CSRF-Token": csrf}
    finally:
        app.extensions["pandrator"]["database"].dispose()


@pytest.mark.parametrize(
    ("created_json", "expected"),
    [
        ("1e400", 0),
        ("-1e400", 0),
        ("1700000000", 1700000000),
        ("1700000000.9", 1700000000),
        ('"1700000000"', 1700000000),
        ("-1", 0),
        ("true", 0),
        ("null", 0),
        ('"invalid"', 0),
        ("{}", 0),
        ("[]", 0),
    ],
)
def test_model_list_normalizes_wrapper_creation_time(
    application: tuple[Flask, FlaskClient, dict[str, str]],
    created_json: str,
    expected: int,
) -> None:
    app, client, headers = application
    wrapper = requests.Response()
    wrapper.status_code = 200
    wrapper._content = (
        '{"data":[{"id":"custom/usable-model","created":'
        + created_json
        + ',"is_local":true,"removable":true}]}'
    ).encode()
    health = requests.Response()
    health.status_code = 200
    health._content = b'{"status":"ok","version":"fixture"}'
    catalogue = {
        "services": [
            {"id": "xtts", "api_base": "http://127.0.0.1:8020", "connection_mode": "external"}
        ]
    }
    with (
        mock.patch.object(
            app.extensions["pandrator"]["tts_catalogue"], "snapshot", return_value=(catalogue, 0)
        ),
        mock.patch.object(requests, "get", side_effect=[wrapper, health]) as get,
    ):
        response = client.get("/api/v1/services/tts/xtts/models", headers=headers)

    assert response.status_code == 200
    payload = response.get_json()
    assert isinstance(payload, dict)
    assert payload["data"][0]["created"] == expected
    assert payload["data"][0]["id"] == "custom/usable-model"
    assert payload["data"][0]["removable"] is True
    assert payload["lifecycle_supported"] is True
    assert payload["wrapper"] == {"status": "ok", "version": "fixture"}
    assert [call.args[0] for call in get.call_args_list] == [
        "http://127.0.0.1:8020/v1/models",
        "http://127.0.0.1:8020/health",
    ]
