"""Service preview admission retains replay identity and queue resource ownership."""

from collections.abc import Iterator
from pathlib import Path
from unittest import mock

import pytest
from flask import Flask
from flask.testing import FlaskClient
from sqlalchemy import select

from pandrator.web.api import create_app
from pandrator.web.models import ApiIdempotency, Job
from tests.web_test_support import prepare_web_test_data_root

PREVIEW = "/api/v1/services/tts/xtts/preview"
BODY = {"text": "A short preview.", "model": "custom/model", "voice": "narrator"}
SETTINGS = {"tts_service": "XTTS", "model": "custom/model", "speaker": "narrator"}


@pytest.fixture
def application(tmp_path: Path) -> Iterator[tuple[Flask, FlaskClient, dict[str, str]]]:
    prepare_web_test_data_root(tmp_path)
    app = create_app(data_root=tmp_path, testing=True)
    _, token = app.extensions["pandrator"]["auth"].create_api_token(
        "Preview fixture", scopes=["app.run"], principal_kind="automation_client"
    )
    try:
        yield app, app.test_client(), {"Authorization": f"Bearer {token}"}
    finally:
        app.extensions["pandrator"]["database"].dispose()


def test_preview_replay_precedes_catalogue_lookup_and_keeps_one_owned_job(
    application: tuple[Flask, FlaskClient, dict[str, str]],
) -> None:
    app, client, authorization = application
    extension = app.extensions["pandrator"]
    headers = {**authorization, "Idempotency-Key": "preview-repeat-fixture"}
    with mock.patch.object(extension["tts_catalogue"], "preview_settings", return_value=SETTINGS):
        first = client.post(PREVIEW, json=BODY, headers=headers)
    assert first.status_code == 202
    with mock.patch.object(
        extension["tts_catalogue"],
        "preview_settings",
        side_effect=AssertionError("Replay must precede catalogue lookup"),
    ):
        replay = client.post(PREVIEW, json=BODY, headers=headers)
        conflict = client.post(PREVIEW, json={**BODY, "text": "Changed preview."}, headers=headers)
    assert replay.status_code == 202
    assert replay.get_json() == first.get_json()
    assert replay.headers["Idempotency-Replayed"] == "true"
    assert conflict.status_code == 409
    assert conflict.get_json()["error"]["code"] == "idempotency_conflict"
    with extension["database"].session() as session:
        jobs = list(session.scalars(select(Job)))
        reservations = list(session.scalars(select(ApiIdempotency)))
        assert len(jobs) == len(reservations) == 1
        assert jobs[0].id == first.get_json()["id"] == reservations[0].resource_id
        assert jobs[0].kind == "tts.preview"
        assert jobs[0].max_attempts == 2
        assert jobs[0].resource_keys_json == ["service:tts:xtts"]
        assert jobs[0].payload_json == {"text": BODY["text"], "settings": SETTINGS}


def test_preview_automation_requires_key_before_catalogue_or_queue(
    application: tuple[Flask, FlaskClient, dict[str, str]],
) -> None:
    app, client, authorization = application
    extension = app.extensions["pandrator"]
    with mock.patch.object(extension["tts_catalogue"], "preview_settings") as settings:
        response = client.post(PREVIEW, json=BODY, headers=authorization)
    assert response.status_code == 400
    assert response.get_json()["error"]["code"] == "idempotency_key_required"
    settings.assert_not_called()
    with extension["database"].session() as session:
        assert list(session.scalars(select(Job))) == []
        assert list(session.scalars(select(ApiIdempotency))) == []
