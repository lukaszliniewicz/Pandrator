"""Client response ownership uses native Requests responses without networking."""

from __future__ import annotations

import io
from pathlib import Path
from unittest import mock

import pytest
import requests

from pandrator_manager import client as client_module
from pandrator_manager.client import ManagerApiError, ManagerClient, ManagerUnavailable
from pandrator_manager.context import WorkspaceLayout
from pandrator_manager.models import ConnectionDescriptor


class ResponseSpy(requests.Response):
    def __init__(
        self,
        body: bytes = b"unconsumed stream",
        *,
        status_code: int = 200,
        instance: str = "fixture-instance",
    ) -> None:
        super().__init__()
        self.close_attempts = 0
        self.status_code = status_code
        self.headers["X-Pandrator-Manager-Instance"] = instance
        self.encoding = "utf-8"
        self.raw = io.BytesIO(body)

    def close(self) -> None:
        self.close_attempts += 1
        super().close()


@pytest.fixture
def transport(tmp_path: Path):
    layout = WorkspaceLayout.from_value(tmp_path)
    descriptor = ConnectionDescriptor(
        manager_version="fixture",
        workspace=str(layout.workspace),
        base_url="http://127.0.0.1:12345",
        instance_id="fixture-instance",
        pid=12345,
        process_create_time=1.0,
        executable="fixture-python",
    )
    session = requests.Session()
    try:
        yield ManagerClient(layout, descriptor, "fixture-secret", session=session), session
    finally:
        session.close()


def test_rejected_identity_closes_unconsumed_stream(transport):
    client, session = transport
    response = ResponseSpy(instance="another-instance")
    with mock.patch.object(session, "request", return_value=response):
        with pytest.raises(ManagerUnavailable, match="identity does not match"):
            client.request("GET", "/v1/events", stream=True)
    assert response.close_attempts == 1
    assert response.raw.closed
    assert not response._content_consumed


def test_successful_stream_is_returned_unchanged_and_caller_owned(transport):
    client, session = transport
    response = ResponseSpy()
    with mock.patch.object(session, "request", return_value=response):
        returned = client.request("GET", "/v1/events", stream=True)
    assert returned is response
    assert response.close_attempts == 0
    assert not response.raw.closed
    assert not response._content_consumed
    returned.close()
    assert response.close_attempts == 1
    assert response.raw.closed


def test_http_json_error_preserves_payload_status_and_message(transport):
    client, session = transport
    response = ResponseSpy(
        b'{"error":{"code":"fixture","message":"primary API failure"}}', status_code=409
    )
    with mock.patch.object(session, "request", return_value=response):
        with pytest.raises(ManagerApiError) as raised:
            client.request("POST", "/v1/plans", json_payload={}, stream=True)
    assert raised.value.status_code == 409
    assert raised.value.payload == {"error": {"code": "fixture", "message": "primary API failure"}}
    assert str(raised.value) == "primary API failure"
    assert response.close_attempts == 1


def test_http_invalid_json_keeps_truncated_text_fallback(transport):
    client, session = transport
    response = ResponseSpy(b"x" * 1100, status_code=503)
    with mock.patch.object(session, "request", return_value=response):
        with pytest.raises(ManagerApiError) as raised:
            client.request("GET", "/v1/status", stream=True)
    assert raised.value.status_code == 503
    assert raised.value.payload == {"error": {"message": "x" * 1000}}
    assert str(raised.value) == "x" * 1000
    assert response.close_attempts == 1


@pytest.mark.parametrize(
    "primary", [RuntimeError("decode failed"), KeyboardInterrupt("interrupted")]
)
def test_unexpected_decode_exception_is_preserved_and_response_closed(transport, primary):
    client, session = transport
    response = ResponseSpy(status_code=500)
    with (
        mock.patch.object(session, "request", return_value=response),
        mock.patch.object(response, "json", side_effect=primary),
        pytest.raises(type(primary)) as raised,
    ):
        client.request("GET", "/v1/status", stream=True)
    assert raised.value is primary
    assert response.close_attempts == 1
    assert response.raw.closed


def test_failure_construction_exception_is_preserved_and_response_closed(transport):
    client, session = transport
    response = ResponseSpy(b'{"error":{"message":"API failure"}}', status_code=500)
    primary = RuntimeError("error construction failed")
    with (
        mock.patch.object(session, "request", return_value=response),
        mock.patch.object(client_module, "ManagerApiError", side_effect=primary),
        pytest.raises(RuntimeError) as raised,
    ):
        client.request("GET", "/v1/status")
    assert raised.value is primary
    assert response.close_attempts == 1


def test_request_level_failure_has_no_response_cleanup(transport):
    client, session = transport
    unused = ResponseSpy()
    primary = requests.ConnectionError("request never returned")
    try:
        with (
            mock.patch.object(session, "request", side_effect=primary),
            pytest.raises(requests.ConnectionError) as raised,
        ):
            client.request("GET", "/v1/status", stream=True)
        assert raised.value is primary
        assert unused.close_attempts == 0
    finally:
        unused.close()


def test_ordinary_close_error_does_not_replace_primary_mismatch(transport):
    client, session = transport
    response = ResponseSpy(instance="another-instance")

    def failing_close():
        response.close_attempts += 1
        raise OSError("cleanup failed")

    try:
        with (
            mock.patch.object(session, "request", return_value=response),
            mock.patch.object(response, "close", side_effect=failing_close),
            pytest.raises(ManagerUnavailable) as raised,
        ):
            client.request("GET", "/v1/events", stream=True)
        assert str(raised.value) == "Manager response identity does not match its descriptor."
        assert response.close_attempts == 1
    finally:
        response.raw.close()


@pytest.mark.parametrize("timeout", [(3.0, 5.0), None])
def test_explicit_timeout_is_forwarded_unchanged(transport, timeout):
    client, session = transport
    response = ResponseSpy()
    try:
        with mock.patch.object(session, "request", return_value=response) as request:
            client.request("GET", "/v1/status", timeout=timeout)
        assert request.call_args.kwargs["timeout"] is timeout
    finally:
        response.close()


def test_default_timeout_auth_and_idempotency_are_preserved(transport):
    client, session = transport
    response = ResponseSpy()
    payload = {"fixture": True}
    try:
        with mock.patch.object(session, "request", return_value=response) as request:
            client.request("POST", "/v1/plans", json_payload=payload, idempotency_key="fixture-key")
        assert request.call_args.args == ("POST", "http://127.0.0.1:12345/v1/plans")
        assert request.call_args.kwargs == {
            "headers": {
                "Authorization": "Bearer fixture-secret",
                "Accept": "application/json",
                "Idempotency-Key": "fixture-key",
            },
            "json": payload,
            "timeout": 30,
            "stream": False,
        }
        assert response.close_attempts == 0
    finally:
        response.close()
