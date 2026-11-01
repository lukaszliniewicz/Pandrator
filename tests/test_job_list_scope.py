"""Job diagnostic filters scope database reads before the result limit."""

from __future__ import annotations

from datetime import timedelta

import pytest

from pandrator.web.accounts.private_work_models import QuickTranscriptionOwner
from pandrator.web.api import create_app
from pandrator.web.auth import BootstrapTokenStore
from pandrator.web.job_identity import job_creation_ownership
from pandrator.web.models import Job, QuickTranscription, Workspace, utcnow
from pandrator.web.openapi import build_openapi_document
from tests.web_test_support import prepare_web_test_data_root


@pytest.fixture(scope="module")
def job_case(tmp_path_factory):
    root = tmp_path_factory.mktemp("scoped-jobs")
    prepare_web_test_data_root(root)
    tokens = BootstrapTokenStore()
    app = create_app(data_root=root, testing=True, bootstrap_tokens=tokens, background_maintenance=False)
    client = app.test_client()
    assert (
        client.post(
            "/api/v1/auth/bootstrap", json={"token": tokens.issue()}
        ).status_code
        == 200
    )
    services = app.extensions["pandrator"]["services"]
    target = services.sessions.create("Target output")
    other = services.sessions.create("Other output")
    now = utcnow()
    with services.database.session() as db:
        target_owner = job_creation_ownership(db, session_id=target.id)
        other_owner = job_creation_ownership(db, session_id=other.id)
        old = Job(
            workspace_id=target_owner.workspace_id,
            project_id=target_owner.project_id,
            kind="dubbing.export",
            session_id=target.id,
            status="succeeded",
            created_at=now - timedelta(days=2),
        )
        newer = Job(
            workspace_id=target_owner.workspace_id,
            project_id=target_owner.project_id,
            kind="dubbing.export",
            session_id=target.id,
            status="succeeded",
            created_at=now - timedelta(days=1),
        )
        private = Job(
            workspace_id=target_owner.workspace_id,
            kind="transcription.quick",
            status="succeeded",
            created_at=now - timedelta(hours=12),
        )
        db.add_all([old, newer, private])
        db.flush()
        transcription = QuickTranscription(
                owner_subject="owner",
                job_id=private.id,
                source_suffix=".wav",
                size_bytes=1,
                sha256="a" * 64,
                expires_at=now + timedelta(days=1),
            )
        db.add(transcription)
        db.flush()
        db.add(
            QuickTranscriptionOwner(
                transcription_id=transcription.id,
                workspace_id=target_owner.workspace_id,
                user_id=db.get(Workspace, target_owner.workspace_id).personal_owner_user_id,
            )
        )
        # Each filter alone still sees >100 newer unrelated jobs. Both filters
        # must be applied in SQL before limiting to recover these old exports.
        for index in range(260):
            db.add(
                Job(
                    workspace_id=other_owner.workspace_id,
                    project_id=other_owner.project_id,
                    kind="dubbing.export",
                    session_id=other.id,
                    status="succeeded",
                    created_at=now + timedelta(seconds=index),
                )
            )
            db.add(
                Job(
                    workspace_id=target_owner.workspace_id,
                    project_id=target_owner.project_id,
                    kind="dubbing.generate",
                    session_id=target.id,
                    status="succeeded",
                    created_at=now + timedelta(seconds=300 + index),
                )
            )
        ids = {"old": old.id, "newer": newer.id, "private": private.id}
    yield app, services, client, tokens, target.id, other.id, ids
    services.database.dispose()


def _ids(items):
    return [item.id for item in items]


def test_queue_and_facade_filter_before_limit(job_case):
    _, services, _, _, target_id, _, ids = job_case
    expected = [ids["newer"], ids["old"]]
    assert _ids(services.jobs.list(session_id=target_id, kind="dubbing.export")) == expected
    assert (
        _ids(services.work.diagnostic_list(session_id=target_id, kind="dubbing.export")) == expected
    )
    assert _ids(services.jobs.list(1, session_id=target_id, kind="dubbing.export")) == expected[:1]
    assert ids["old"] not in _ids(services.jobs.list())
    assert all(job.session_id == target_id for job in services.jobs.list(session_id=target_id))
    assert all(job.kind == "dubbing.export" for job in services.jobs.list(kind="dubbing.export"))


def test_http_filter_before_limit_matches_facade(job_case):
    _, services, client, _, target_id, _, _ = job_case
    response = client.get(
        "/api/v1/jobs", query_string={"session_id": target_id, "kind": "dubbing.export"}
    )
    assert response.status_code == 200
    expected = _ids(services.work.diagnostic_list(session_id=target_id, kind="dubbing.export"))
    assert [item["id"] for item in response.get_json()["items"]] == expected
    assert all(
        item["kind"] == "dubbing.export" and item["session_id"] == target_id
        for item in response.get_json()["items"]
    )
    limited = client.get(
        "/api/v1/jobs", query_string={"session_id": target_id, "kind": "dubbing.export", "limit": 1}
    )
    assert [item["id"] for item in limited.get_json()["items"]] == expected[:1]


@pytest.mark.parametrize("session_id", ["", "missing-session"])
def test_empty_and_unknown_sessions_do_not_fall_back_to_global(job_case, session_id):
    _, services, client, _, _, _, _ = job_case
    assert services.jobs.list(session_id=session_id) == []
    assert services.work.diagnostic_list(session_id=session_id) == []
    assert (
        client.get("/api/v1/jobs", query_string={"session_id": session_id}).get_json()["items"]
        == []
    )


@pytest.mark.parametrize(
    "kind", ["", "dubbing.export ", "export", "dubbing.export,dubbing.generate"]
)
def test_kind_is_an_exact_filter(job_case, kind):
    _, services, client, _, target_id, _, _ = job_case
    assert services.jobs.list(session_id=target_id, kind=kind) == []
    assert (
        client.get("/api/v1/jobs", query_string={"session_id": target_id, "kind": kind}).get_json()[
            "items"
        ]
        == []
    )


def test_unfiltered_shape_order_and_limit_normalization_are_preserved(job_case):
    _, services, client, _, _, _, _ = job_case
    jobs = services.jobs.list()
    assert len(jobs) == 100
    assert [job.created_at for job in jobs] == sorted(
        (job.created_at for job in jobs), reverse=True
    )
    assert _ids(services.work.diagnostic_list()) == _ids(jobs)
    response = client.get("/api/v1/jobs")
    assert set(response.get_json()) == {"items"}
    assert [item["id"] for item in response.get_json()["items"]] == _ids(jobs)
    assert "payload_json" in response.get_json()["items"][0]
    assert len(services.jobs.list(0)) == len(services.jobs.list(-20)) == 1
    assert len(services.jobs.list(1000)) == 500
    assert len(client.get("/api/v1/jobs?limit=0").get_json()["items"]) == 1
    assert len(client.get("/api/v1/jobs?limit=invalid").get_json()["items"]) == 100


def test_http_authentication_and_stable_private_owner_across_credentials(job_case):
    app, services, client, tokens, target_id, _, ids = job_case
    query = {"kind": "transcription.quick"}
    assert app.test_client().get("/api/v1/jobs", query_string=query).status_code == 401
    second = app.test_client()
    _, raw_token = services.auth.create_api_token(
        "Separate diagnostic credential", scopes=["app.read"], subject="other-owner"
    )
    second_headers = {"Authorization": f"Bearer {raw_token}"}
    assert (
        second.get(
            "/api/v1/jobs", query_string=query, headers=second_headers
        ).status_code
        == 200
    )
    # Distinct credential subjects belong to the same actual account.
    assert ids["private"] in _ids(
        services.work.diagnostic_list(kind="transcription.quick")
    )
    assert ids["private"] in [
        item["id"] for item in client.get("/api/v1/jobs", query_string=query).get_json()["items"]
    ]
    assert [
        item["id"]
        for item in second.get(
            "/api/v1/jobs", query_string=query, headers=second_headers
        ).get_json()["items"]
    ] == [ids["private"]]
    assert second.get(f"/api/v1/jobs/{ids['private']}", headers=second_headers).status_code == 200


def test_jobs_get_openapi_documents_optional_filters():
    operation = build_openapi_document()["paths"]["/api/v1/jobs"]["get"]
    parameters = {parameter["name"]: parameter for parameter in operation["parameters"]}
    assert parameters["session_id"]["schema"] == {"type": "string"}
    assert parameters["kind"]["schema"] == {"type": "string"}
    assert parameters["limit"]["schema"] == {
        "type": "integer",
        "minimum": 1,
        "maximum": 500,
        "default": 100,
    }
    assert all(
        parameter["in"] == "query" and not parameter.get("required")
        for parameter in parameters.values()
    )
