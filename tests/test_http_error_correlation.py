"""Unexpected failures remain attributable without logging request secrets."""

import logging
from unittest.mock import patch

import pytest

from pandrator.web.api import create_app
from pandrator.web.auth import BootstrapTokenStore
from tests.web_test_support import prepare_web_test_data_root


@pytest.fixture
def error_app(tmp_path):
    prepare_web_test_data_root(str(tmp_path))
    tokens = BootstrapTokenStore()
    app = create_app(
        data_root=str(tmp_path),
        testing=False,
        bootstrap_tokens=tokens,
        background_maintenance=False,
    )

    @app.get("/api/v1/error-fixture/<item_id>")
    def error_fixture(item_id):
        raise RuntimeError("Injected failure")

    client = app.test_client()
    client.post("/api/v1/auth/bootstrap", json={"token": tokens.issue()})
    yield app, client
    app.extensions["pandrator"]["database"].dispose()


def test_unexpected_error_is_correlated_and_query_is_private(error_app, caplog):
    app, client = error_app
    caplog.set_level(logging.ERROR, logger=app.logger.name)
    response = client.get(
        "/api/v1/error-fixture/private-item?token=private-query",
        headers={"X-Request-ID": "correlation-fixture-001"},
    )
    assert response.status_code == 500
    assert response.get_json()["error"]["request_id"] == "correlation-fixture-001"
    assert "request_id=correlation-fixture-001 method=GET" in caplog.text
    assert "route=/api/v1/error-fixture/<item_id>" in caplog.text
    assert "timestamp=" in caplog.text
    assert "Injected failure" in caplog.text
    assert "private-query" not in caplog.text
    assert "private-item" not in caplog.text


def test_audit_failure_has_same_request_correlation(error_app, caplog):
    app, client = error_app
    caplog.set_level(logging.ERROR, logger=app.logger.name)
    with patch.object(
        app.extensions["pandrator"]["audit"],
        "record",
        side_effect=RuntimeError("Injected audit failure"),
    ):
        response = client.get(
            "/api/v1/health", headers={"X-Request-ID": "audit-correlation-001"}
        )
    assert response.status_code == 200
    assert "Audit projection failed" in caplog.text
    assert "request_id=audit-correlation-001 method=GET route=/api/v1/health" in caplog.text
