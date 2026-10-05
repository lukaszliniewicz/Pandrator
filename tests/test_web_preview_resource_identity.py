"""Accepted spellings of one preview service must share queue resource claims."""

from collections.abc import Iterator
from pathlib import Path
from unittest import mock

import pytest
import requests
from flask import Flask
from flask.testing import FlaskClient
from sqlalchemy import select

from pandrator.web.api import create_app
from pandrator.web.auth import BootstrapTokenStore
from pandrator.web.models import ResourceClaim
from tests.web_test_support import prepare_web_test_data_root


@pytest.fixture
def application(tmp_path: Path) -> Iterator[tuple[Flask, FlaskClient, dict[str, str]]]:
    prepare_web_test_data_root(tmp_path)
    bootstrap = BootstrapTokenStore()
    token = bootstrap.issue()
    with mock.patch.object(
        requests.sessions.Session,
        "request",
        side_effect=AssertionError("Provider network forbidden"),
    ):
        app = create_app(data_root=tmp_path, testing=True, bootstrap_tokens=bootstrap)
        client = app.test_client()
        csrf = client.post("/api/v1/auth/bootstrap", json={"token": token}).get_json()["csrf_token"]
        try:
            yield app, client, {"X-CSRF-Token": csrf}
        finally:
            app.extensions["pandrator"]["database"].dispose()


@pytest.mark.parametrize("use_key", [False, True], ids=["browser", "idempotent"])
@pytest.mark.parametrize(
    ("first_service", "second_service", "canonical_first", "canonical_second"),
    [
        ("xtts", "xtts", "xtts", "xtts"),
        ("xtts", "XTTS", "xtts", "xtts"),
        ("xtts", "XtTs", "xtts", "xtts"),
        ("xtts", "kokoro", "xtts", "kokoro"),
        ("audio_cpp", "audio-cpp", "audio_cpp", "audio_cpp"),
    ],
)
def test_preview_queue_resource_identity(
    application: tuple[Flask, FlaskClient, dict[str, str]],
    use_key: bool,
    first_service: str,
    second_service: str,
    canonical_first: str,
    canonical_second: str,
) -> None:
    app, client, csrf_headers = application
    extension = app.extensions["pandrator"]
    queue = extension["jobs"]
    posted = []
    for index, service in enumerate([first_service, second_service]):
        headers = dict(csrf_headers)
        if use_key:
            headers["Idempotency-Key"] = f"resource-fixture-{index}"
        response = client.post(
            f"/api/v1/services/tts/{service}/preview",
            json={"text": "A resource identity preview."},
            headers=headers,
        )
        assert response.status_code == 202, response.get_json()
        posted.append(response.get_json())

    assert posted[0]["payload_json"]["settings"]["preview_service_id"] == canonical_first
    assert posted[1]["payload_json"]["settings"]["preview_service_id"] == canonical_second
    first = queue.claim("fixture-first")
    second = queue.claim("fixture-second")
    assert first is not None and second is not None
    assert [first.id, second.id] == [job["id"] for job in posted]
    try:
        assert queue.acquire_resources(
            first.id,
            "fixture-first",
            first.resource_keys_json,
            lease_generation=first.lease_generation,
        )
        allowed = queue.acquire_resources(
            second.id,
            "fixture-second",
            second.resource_keys_json,
            lease_generation=second.lease_generation,
        )
        assert allowed is (canonical_second != canonical_first)
        assert first.resource_keys_json == [f"service:tts:{canonical_first}"]
        assert second.resource_keys_json == [f"service:tts:{canonical_second}"]
    finally:
        queue.release_resources(first.id, "fixture-first", lease_generation=first.lease_generation)
        queue.release_resources(
            second.id, "fixture-second", lease_generation=second.lease_generation
        )
    with extension["database"].session() as session:
        assert list(session.scalars(select(ResourceClaim))) == []
