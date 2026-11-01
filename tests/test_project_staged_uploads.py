"""Real disposable HTTP/SQLite contracts for project-bound staged source uploads."""

from __future__ import annotations

import hashlib
import io
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

import pytest
from sqlalchemy import select

from pandrator.web import models as m
from pandrator.web.public_link_http import cookie_name
from tests.test_account_authentication_integration import case as case  # noqa: F401
from tests.test_provider_workspace_access import no_network as no_network  # noqa: F401


def post(case, route, **kwargs):
    return case.client.post(route, headers={"X-CSRF-Token": case.csrf}, **kwargs)


def session(case, name="Staged upload"):
    response = post(case, "/api/v1/sessions", json={"name": name, "workflow_kind": "audiobook"})
    assert response.status_code == 201
    return response.get_json()


def multipart(case, session_id, *, attach=None, purpose=None, client=None, headers=None):
    data = {
        "file": (
            io.BytesIO(b"Original source." if attach is None else b"New staged source."),
            "staged.txt",
        )
    }
    if session_id:
        data["session_id"] = session_id
    if attach is not None:
        data["attach_to_session"] = attach
    if purpose is not None:
        data["purpose"] = purpose
    return (client or case.client).post(
        "/api/v1/uploads", data=data, headers=headers or {"X-CSRF-Token": case.csrf}
    )


def selected_state(case, session_id):
    with case.database.snapshot_session() as unit:
        record = unit.get_one(m.SessionRecord, session_id)
        return (
            record.revision,
            [
                (row.id, row.source_asset_id, row.is_current)
                for row in unit.scalars(
                    select(m.SessionSource)
                    .where(m.SessionSource.session_id == session_id)
                    .order_by(m.SessionSource.id)
                )
            ],
            [
                (row.stage_key, row.artifact_id, row.revision)
                for row in unit.scalars(
                    select(m.SessionStageSelection)
                    .where(m.SessionStageSelection.session_id == session_id)
                    .order_by(m.SessionStageSelection.stage_key)
                )
            ],
            [
                (row.id, row.artifact_id)
                for row in unit.scalars(
                    select(m.SourceRecord).where(m.SourceRecord.session_id == session_id)
                )
            ],
        )


def guest(case, record):
    configured = case.client.put(
        "/api/v1/admin/public-sharing",
        json={"enabled": True, "public_origin": "http://localhost", "expected_revision": 0},
        headers={"X-CSRF-Token": case.csrf},
    )
    assert configured.status_code == 200
    response = post(
        case,
        f"/api/v1/projects/{record['project_id']}/links",
        json={"label": "Source editor", "mode": "manage_project", "request_key": str(uuid4())},
    )
    assert response.status_code == 200
    created = response.get_json()
    client = case.app.test_client()
    secret = parse_qs(urlsplit(created["url"]).fragment)["token"][0]
    redeemed = client.post(
        f"/api/v1/public-links/{created['link']['id']}/redeem",
        json={"token": secret},
        headers={"Origin": "http://localhost"},
    )
    assert redeemed.status_code == 200
    assert client.get_cookie(cookie_name(created["link"]["id"]), path="/api/v1") is not None
    return (
        client,
        {
            "X-Pandrator-Project-Link": created["link"]["id"],
            "X-CSRF-Token": redeemed.get_json()["csrf_token"],
        },
        created["link"]["id"],
    )


def test_default_and_staged_multipart_leave_selection_unchanged_until_confirmation(case):
    record = session(case)
    original = multipart(case, record["id"])
    assert original.status_code == 201 and original.get_json()["attachment"] is not None
    before = selected_state(case, record["id"])
    client, headers, _link_id = guest(case, record)
    staged = multipart(case, record["id"], attach="false", client=client, headers=headers)
    assert staged.status_code == 201
    result = staged.get_json()
    assert result["attachment"] is None and result["source_asset_id"]
    assert selected_state(case, record["id"]) == before
    with case.database.snapshot_session() as unit:
        artifact = unit.get_one(m.Artifact, result["artifact_id"])
        assert artifact.session_id == record["id"] and artifact.project_id == record["project_id"]
        assert unit.get_one(m.Project, artifact.project_id).workspace_id == artifact.workspace_id
        assert unit.get_one(m.SourceAsset, result["source_asset_id"]).artifact_id == artifact.id
    preview = client.post(
        f"/api/v1/sessions/{record['id']}/sources/change-preview",
        json={"role": "primary", "new_source_asset_id": result["source_asset_id"]},
        headers=headers,
    )
    assert preview.status_code == 200
    impact = preview.get_json()
    for extra in [{"nested": {"impact_token": "a" * 64}}, {"api_token": "a" * 64}]:
        refused = client.post(
            f"/api/v1/sessions/{record['id']}/sources/change",
            json={
                "role": "primary",
                "new_source_asset_id": result["source_asset_id"],
                "expected_revision": before[0],
                "impact_token": impact["impact_token"],
                **extra,
            },
            headers=headers | {"Idempotency-Key": "unsafe-impact-receipt"},
        )
        assert refused.status_code == 403
    assert (
        client.post(
            f"/api/v1/sessions/{record['id']}/sources/change-preview",
            json={
                "role": "primary",
                "new_source_asset_id": result["source_asset_id"],
                "impact_token": impact["impact_token"],
            },
            headers=headers,
        ).status_code
        == 403
    )
    changed = client.post(
        f"/api/v1/sessions/{record['id']}/sources/change",
        json={
            "role": "primary",
            "new_source_asset_id": result["source_asset_id"],
            "expected_revision": before[0],
            "impact_token": impact["impact_token"],
        },
        headers=headers | {"Idempotency-Key": "staged-source-confirm"},
    )
    assert changed.status_code == 200
    after = selected_state(case, record["id"])
    assert after[0] > before[0]
    assert result["source_asset_id"] in {asset_id for _, asset_id, current in after[1] if current}


def test_staged_chunked_replay_and_default_digest_compatibility(case):
    record = session(case)
    multipart(case, record["id"])
    before = selected_state(case, record["id"])
    client, headers, _link_id = guest(case, record)
    data = b"Chunked project candidate."
    body = {
        "filename": "candidate.txt",
        "size_bytes": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
        "session_id": record["id"],
        "attach_to_session": False,
    }
    initialized = client.post(
        "/api/v1/uploads/init",
        json=body,
        headers=headers | {"Idempotency-Key": "staged-chunk-init"},
    )
    assert initialized.status_code == 201
    upload_id = initialized.get_json()["id"]
    assert (
        client.put(
            f"/api/v1/uploads/{upload_id}/chunks/0",
            data=data,
            content_type="application/octet-stream",
            headers=headers,
        ).status_code
        == 200
    )
    completed = client.post(f"/api/v1/uploads/{upload_id}/complete", headers=headers)
    assert completed.status_code == 201
    result = completed.get_json()
    assert result["attachment"] is None
    assert selected_state(case, record["id"]) == before
    assert (
        client.post(f"/api/v1/uploads/{upload_id}/complete", headers=headers).get_json() == result
    )
    assert (
        client.get(f"/api/v1/uploads/{upload_id}", headers=headers).get_json()["result"] == result
    )
    with case.database.snapshot_session() as unit:
        row = unit.get_one(m.UploadSessionRecord, upload_id)
        assert row.attach_to_session is False
        artifact = unit.get_one(m.Artifact, result["artifact_id"])
        assert artifact.session_id == record["id"] and artifact.project_id == record["project_id"]
    default_body = {key: value for key, value in body.items() if key != "attach_to_session"}
    normal = client.post(
        "/api/v1/uploads/init",
        json=default_body,
        headers=headers | {"Idempotency-Key": "default-chunk-init"},
    )
    explicit = client.post(
        "/api/v1/uploads/init",
        json=default_body | {"attach_to_session": True},
        headers=headers | {"Idempotency-Key": "default-chunk-init"},
    )
    assert (
        normal.status_code == explicit.status_code == 201
        and normal.get_json() == explicit.get_json()
    )
    assert explicit.headers.get("Idempotency-Replayed") == "true"
    preview = client.post(
        f"/api/v1/sessions/{record['id']}/sources/change-preview",
        json={
            "role": "primary",
            "new_source_asset_id": result["source_asset_id"],
        },
        headers=headers,
    )
    assert preview.status_code == 200
    changed = client.post(
        f"/api/v1/sessions/{record['id']}/sources/change",
        json={
            "role": "primary",
            "new_source_asset_id": result["source_asset_id"],
            "expected_revision": before[0],
            "impact_token": preview.get_json()["impact_token"],
        },
        headers=headers | {"Idempotency-Key": "consume-staged-chunk"},
    )
    assert changed.status_code == 200
    with case.database.snapshot_session() as unit:
        artifact = unit.get_one(m.Artifact, result["artifact_id"])
        assert artifact.session_id is None and artifact.project_id == record["project_id"]
    replay = client.post(f"/api/v1/uploads/{upload_id}/complete", headers=headers)
    assert replay.status_code == 201
    assert replay.get_json() == result
    status = client.get(f"/api/v1/uploads/{upload_id}", headers=headers)
    assert status.status_code == 200 and status.get_json()["result"] == result


@pytest.mark.parametrize("value", ["False", "0", "", "yes"])
def test_multipart_flag_is_strict_and_contextless_or_cover_staging_denied(case, value):
    record = session(case)
    assert multipart(case, record["id"], attach=value).status_code == 422
    assert multipart(case, None, attach="false").status_code == 422
    assert multipart(case, record["id"], attach="false", purpose="cover").status_code == 422
    assert (
        post(
            case,
            "/api/v1/uploads/init",
            json={"filename": "a.txt", "size_bytes": 1, "attach_to_session": False},
        ).status_code
        == 422
    )
    assert (
        post(
            case,
            "/api/v1/uploads/init",
            json={
                "filename": "a.txt",
                "size_bytes": 1,
                "session_id": record["id"],
                "attach_to_session": "false",
            },
        ).status_code
        == 422
    )


def test_guest_staging_denies_foreign_revoked_and_stale_receipts(case):
    own, foreign = session(case), session(case, "Other project")
    multipart(case, own["id"])
    foreign_asset = multipart(case, foreign["id"], attach="false").get_json()["source_asset_id"]
    client, headers, link_id = guest(case, own)
    assert multipart(
        case, foreign["id"], attach="false", client=client, headers=headers
    ).status_code in {403, 404}
    assert client.post(
        f"/api/v1/sessions/{own['id']}/sources/change-preview",
        json={"role": "primary", "new_source_asset_id": foreign_asset},
        headers=headers,
    ).status_code in {403, 404}
    staged = multipart(case, own["id"], attach="false", client=client, headers=headers).get_json()
    before = selected_state(case, own["id"])
    impact = client.post(
        f"/api/v1/sessions/{own['id']}/sources/change-preview",
        json={"role": "primary", "new_source_asset_id": staged["source_asset_id"]},
        headers=headers,
    ).get_json()
    with case.database.immediate_session() as unit:
        unit.get_one(m.SessionRecord, own["id"]).revision += 1
    changed = client.post(
        f"/api/v1/sessions/{own['id']}/sources/change",
        json={
            "role": "primary",
            "new_source_asset_id": staged["source_asset_id"],
            "expected_revision": before[0],
            "impact_token": impact["impact_token"],
        },
        headers=headers | {"Idempotency-Key": "stale-stage-confirm"},
    )
    assert changed.status_code == 409
    revoked = post(case, f"/api/v1/project-links/{link_id}/revoke", json={"expected_revision": 1})
    assert revoked.status_code == 200
    assert multipart(
        case, own["id"], attach="false", client=client, headers=headers
    ).status_code in {401, 403}


def test_chunk_writer_rechecks_attachment_flag_and_staged_result_roots(case, monkeypatch):
    from pandrator.web.upload_identity import validate_upload_result

    record = session(case)
    services = case.app.extensions["pandrator"]["services"]
    initialized = post(
        case,
        "/api/v1/uploads/init",
        json={
            "filename": "candidate.txt",
            "size_bytes": 3,
            "session_id": record["id"],
            "attach_to_session": False,
        },
    ).get_json()
    upload_id = initialized["id"]
    assert (
        case.client.put(
            f"/api/v1/uploads/{upload_id}/chunks/0",
            data=b"abc",
            headers={"X-CSRF-Token": case.csrf},
        ).status_code
        == 200
    )
    original = services.artifacts.prepare_registration

    def change_flag(*args, **kwargs):
        result = original(*args, **kwargs)
        with case.database.immediate_session() as unit:
            unit.get_one(m.UploadSessionRecord, upload_id).attach_to_session = True
        return result

    monkeypatch.setattr(services.artifacts, "prepare_registration", change_flag)
    assert post(case, f"/api/v1/uploads/{upload_id}/complete").status_code == 409
    with case.database.snapshot_session() as unit:
        row = unit.get_one(m.UploadSessionRecord, upload_id)
        assert row.state == "open" and "0" in row.received_json
    monkeypatch.setattr(services.artifacts, "prepare_registration", original)
    with case.database.immediate_session() as unit:
        unit.get_one(m.UploadSessionRecord, upload_id).attach_to_session = False
    result = post(case, f"/api/v1/uploads/{upload_id}/complete").get_json()
    with case.database.immediate_session() as unit:
        row = unit.get_one(m.UploadSessionRecord, upload_id)
        row.result_json = result | {"attachment": {"id": "not-null"}}
        with pytest.raises(ValueError, match="null attachment"):
            validate_upload_result(unit, row)
        row.result_json = result
    foreign = session(case, "Other receipt project")
    with case.database.immediate_session() as unit:
        row = unit.get_one(m.UploadSessionRecord, upload_id)
        artifact = unit.get_one(m.Artifact, result["artifact_id"])
        original_project = artifact.project_id
        artifact.session_id = None
        artifact.project_id = foreign["project_id"]
        with pytest.raises(ValueError, match="another project"):
            validate_upload_result(unit, row)
        artifact.project_id = original_project
        # Detachment is valid, but another non-null session remains invalid
        # even when that language session shares the original project root.
        peer = m.SessionRecord(project_id=original_project, name="Other language session")
        unit.add(peer)
        unit.flush()
        artifact.session_id = peer.id
        with pytest.raises(ValueError, match="another session"):
            validate_upload_result(unit, row)
        artifact.session_id = None
        validate_upload_result(unit, row)


def test_fresh_history_and_staged_flag_downgrade_guard(case):
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from sqlalchemy import inspect, text

    from tests.test_public_link_foundation import migration

    with case.database.engine.connect() as connection:
        assert (
            connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
            == "0087_public_project_links"
        )
        column = next(
            item
            for item in inspect(connection).get_columns("upload_sessions")
            if item["name"] == "attach_to_session"
        )
        assert not column["nullable"] and str(column["default"]).strip("()'") == "1"
    record = session(case)
    initialized = post(
        case,
        "/api/v1/uploads/init",
        json={
            "filename": "candidate.txt",
            "size_bytes": 3,
            "session_id": record["id"],
            "attach_to_session": False,
        },
    )
    assert initialized.status_code == 201
    with pytest.raises(RuntimeError, match="staged upload"):
        with (
            case.database.engine.begin() as connection,
            Operations.context(MigrationContext.configure(connection)),
        ):
            migration().downgrade()
