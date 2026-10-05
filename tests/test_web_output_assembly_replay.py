"""Accepted assembly requests replay before consulting mutable preparation inputs."""

from collections.abc import Iterator
from pathlib import Path
from unittest import mock

import pytest
import requests
from flask import Flask
from flask.testing import FlaskClient
from sqlalchemy import select

from pandrator.web.api import create_app
from pandrator.web.models import ApiIdempotency, Job, OutputAssembly
from tests.web_test_support import prepare_web_test_data_root


@pytest.fixture
def application(tmp_path: Path) -> Iterator[tuple[Flask, FlaskClient, dict[str, str], str]]:
    prepare_web_test_data_root(tmp_path)
    with mock.patch.object(
        requests.sessions.Session,
        "request",
        side_effect=AssertionError("Provider network forbidden"),
    ):
        app = create_app(data_root=tmp_path, testing=True)
        extension = app.extensions["pandrator"]
        _, token = extension["auth"].create_api_token(
            "Assembly replay fixture",
            scopes=["app.read", "app.write", "app.run"],
            principal_kind="automation_client",
        )
        headers = {"Authorization": f"Bearer {token}"}
        client = app.test_client()
        try:
            created = client.post(
                "/api/v1/sessions",
                json={"name": "Assembly replay fixture", "workflow_kind": "audiobook"},
                headers={**headers, "Idempotency-Key": "assembly-fixture-session"},
            )
            assert created.status_code == 201, created.get_json()
            session_id = created.get_json()["id"]
            extension["generation"].create_plan(
                session_id, source_revision_id=None, segments=[{"text": "An assembly fixture."}]
            )
            yield app, client, headers, session_id
        finally:
            extension["database"].dispose()


def assert_assembly_count(app: Flask, expected: int) -> None:
    with app.extensions["pandrator"]["database"].session() as session:
        assemblies = list(session.scalars(select(OutputAssembly)))
        jobs = list(session.scalars(select(Job).where(Job.kind == "generation.assemble")))
        reservations = list(
            session.scalars(
                select(ApiIdempotency).where(ApiIdempotency.operation_id == "createOutputAssembly")
            )
        )
        assert len(assemblies) == len(jobs) == len(reservations) == expected
        if expected:
            assert assemblies[0].job_id == jobs[0].id
            assert reservations[0].resource_id == assemblies[0].id
            assert reservations[0].state == "completed"


@pytest.mark.parametrize("failure", [KeyError("session"), ValueError("output settings changed")])
def test_assembly_replay_precedes_preparation_failures(
    application: tuple[Flask, FlaskClient, dict[str, str], str], failure: Exception
) -> None:
    app, client, authorization, session_id = application
    uri = f"/api/v1/sessions/{session_id}/output-assemblies"
    headers = {**authorization, "Idempotency-Key": "assembly-replay-fixture"}
    first = client.post(uri, json={}, headers=headers)
    assert first.status_code == 202, first.get_json()
    # Inject the two production preparation failure types, without claiming a
    # real session purge or settings mutation occurred in this fixture.
    with mock.patch.object(
        app.extensions["pandrator"]["generation"], "prepare_assembly", side_effect=failure
    ) as preparation:
        replay = client.post(uri, json={}, headers=headers)
    assert replay.status_code == 202, replay.get_json()
    assert replay.get_json() == first.get_json()
    assert replay.headers["Idempotency-Replayed"] == "true"
    preparation.assert_not_called()
    assert_assembly_count(app, 1)


def test_assembly_changed_request_conflicts_before_output_format_validation(
    application: tuple[Flask, FlaskClient, dict[str, str], str],
) -> None:
    app, client, authorization, session_id = application
    uri = f"/api/v1/sessions/{session_id}/output-assemblies"
    headers = {**authorization, "Idempotency-Key": "assembly-conflict-fixture"}
    first = client.post(uri, json={}, headers=headers)
    assert first.status_code == 202, first.get_json()
    conflict = client.post(
        uri,
        json={"run_override": {"output": {"format": "unsupported-fixture"}}},
        headers=headers,
    )
    assert conflict.status_code == 409
    assert conflict.get_json()["error"]["code"] == "idempotency_conflict"
    assert_assembly_count(app, 1)


def test_fresh_invalid_assembly_retains_validation_and_no_durable_reservation(
    application: tuple[Flask, FlaskClient, dict[str, str], str],
) -> None:
    app, client, authorization, session_id = application
    response = client.post(
        f"/api/v1/sessions/{session_id}/output-assemblies",
        json={"run_override": {"output": {"format": "unsupported-fixture"}}},
        headers={**authorization, "Idempotency-Key": "assembly-invalid-fixture"},
    )
    assert response.status_code == 409
    assert response.get_json()["error"]["code"] == "assembly_unavailable"
    assert_assembly_count(app, 0)


def test_automation_assembly_requires_key_before_preparation(
    application: tuple[Flask, FlaskClient, dict[str, str], str],
) -> None:
    app, client, authorization, session_id = application
    with mock.patch.object(
        app.extensions["pandrator"]["generation"], "prepare_assembly"
    ) as prepare:
        response = client.post(
            f"/api/v1/sessions/{session_id}/output-assemblies", json={}, headers=authorization
        )
    assert response.status_code == 400
    assert response.get_json()["error"]["code"] == "idempotency_key_required"
    prepare.assert_not_called()
    assert_assembly_count(app, 0)
