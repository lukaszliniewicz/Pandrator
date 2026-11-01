"""Passive source cleaning across real named HTTP admission and worker leases."""

from __future__ import annotations

import io
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4

import pytest
from flask.wrappers import Request
from sqlalchemy import event, func, select

from pandrator.logic.source_cleaning import SourceBlock, SourceDocument
from pandrator.logic.source_cleaning.deterministic import EpubExtractionResult
from pandrator.web import models as m
from pandrator.web.jobs import Worker
from tests.test_account_authentication_integration import case as case  # noqa: F401
from tests.test_evidence_access_routes import body, headers
from tests.test_provider_workspace_access import no_network as no_network  # noqa: F401
from tests.test_provider_workspace_access import scoped_token
from tests.test_source_library_workspace_http import library as library  # noqa: F401


def seed(f, client, csrf, *, workspace=None, run_client=None, run_headers=None):
    values = {
        "name": "Passive shared source",
        "workflow_kind": "audiobook",
        "source_language": "en",
    }
    if workspace is not None:
        values["workspace_id"] = workspace
    record = body(
        client.post("/api/v1/sessions", json=values, headers=headers(csrf, str(uuid4()))), 201
    )
    upload = body(
        client.post(
            "/api/v1/uploads",
            data={
                "session_id": record["id"],
                "file": (io.BytesIO(b"synthetic-epub-input"), "book.epub"),
            },
            headers=csrf,
        ),
        201,
    )
    url = f"/api/v1/sessions/{record['id']}/source-cleaning-dispatch-runs"
    values = {
        "source_artifact_id": upload["artifact_id"],
        "instructions": "Preserve the narrative.",
    }
    run = body(
        (run_client or client).post(
            url, json=values, headers=headers(run_headers or csrf, str(uuid4()))
        ),
        202,
    )
    return SimpleNamespace(record=record, run=run, url=url, values=values)


def prepare(f, seed, monkeypatch):
    document = SourceDocument(
        source_type="epub_cleaned_text",
        source_path="book.epub",
        filename="book.epub",
        language="en",
        blocks=[
            SourceBlock(
                block_id="heading",
                text="Chapter One",
                line_start=1,
                line_end=1,
                source_index=0,
                href="chapter.xhtml",
                tag="h1",
                role_candidates=["chapter_heading"],
            ),
            SourceBlock(
                block_id="body",
                text="The narra tive begins here, with a complete sentence.",
                line_start=2,
                line_end=2,
                source_index=1,
                href="chapter.xhtml",
                tag="p",
            ),
        ],
    )
    monkeypatch.setattr(
        "pandrator.web.source_cleaning_dispatch.extract_epub_with_diagnostics",
        lambda *_a, **_kw: EpubExtractionResult.success(document.plain_text()),
    )
    monkeypatch.setattr(
        "pandrator.web.source_cleaning_dispatch.source_cleaning.build_cleaned_epub_source_document",
        lambda *_a, **_kw: document,
    )
    monkeypatch.setattr(
        "pandrator.web.source_cleaning_dispatch.source_cleaning.propose_embedded_chapter_operations",
        lambda *_a, **_kw: [],
    )
    monkeypatch.setattr(
        "pandrator.web.provider_settings.build_llm_settings",
        Mock(side_effect=AssertionError("Passive source cleaning called a provider")),
    )
    assert Worker(
        f.services.jobs, "named-source-cleaning-worker", f.services.workflow_handlers.handlers()
    ).run_once()
    job = f.services.jobs.get(seed.run["job_id"])
    assert job.status == "succeeded", job.error_message
    return body(f.u.bob.get(f"/api/v1/source-cleaning-dispatch-runs/{seed.run['id']}"))


def claim(client, csrf, run_id, key=None):
    return body(
        client.post(
            f"/api/v1/source-cleaning-dispatch-runs/{run_id}/claim",
            json={"lease_seconds": 900},
            headers=headers(csrf, key or str(uuid4())),
        )
    )


def submission(claimed, operations=None):
    return {
        "lease_token": claimed["lease_token"],
        "result": {
            "kind": "source_cleaning",
            "phase": claimed["task"]["phase"],
            "decisions": [],
            "operations": operations or [],
            "summary": "Reviewed this phase.",
            "confidence": 0.9,
        },
    }


@pytest.mark.parametrize("actor", ["private_viewer", "foreign_admin", "read_only_member"])
def test_dispatch_denials_precede_json_or_private_batch_bodies(library, monkeypatch, actor):
    f, u = library, library.u
    seeded = seed(f, u.bob, u.bh, workspace=u.workspace_id if actor == "read_only_member" else None)
    ready = prepare(f, seeded, monkeypatch)
    batch_id, run_id = ready["batches"][0]["id"], seeded.run["id"]
    if actor == "private_viewer":
        with f.case.database.immediate_session() as unit:
            unit.add(
                m.ProjectMembership(
                    project_id=seeded.record["project_id"], user_id=u.alice_id, role="viewer"
                )
            )
        client, csrf, status = u.alice, u.ah, 403
    elif actor == "foreign_admin":
        client, csrf, status = u.owner, u.oh, 404
    else:
        _, csrf = scoped_token(f.case, u.alice_id, {"app.read"}, workspace=u.workspace_id)
        client, status = f.case.app.test_client(), 403
    for url in (seeded.url, f"/api/v1/source-cleaning-dispatch-runs/{run_id}"):
        assert client.get(url, headers=csrf).status_code == (404 if status == 404 else 200)

    def forbidden(*_args, **_kwargs):
        pytest.fail("A denied passive dispatch request parsed JSON")

    monkeypatch.setattr(Request, "get_json", forbidden)
    for url in [
        seeded.url,
        f"/api/v1/source-cleaning-dispatch-runs/{run_id}/claim",
        *[
            f"/api/v1/source-cleaning-dispatch-batches/{batch_id}/{suffix}"
            for suffix in ("renew", "release", "submit", "inspect")
        ],
    ]:
        response = client.post(url, json={}, headers=headers(csrf, str(uuid4())))
        assert response.status_code == status, (url, response.get_json())


def test_two_members_prepare_inspect_repair_finalize_and_read_after_creator_departure(
    library, monkeypatch
):
    f, u = library, library.u
    seeded = seed(f, u.alice, u.ah, workspace=u.workspace_id)
    ready = prepare(f, seeded, monkeypatch)
    assert ready["status"] == "ready" and ready["batch_count"] == 6
    with f.case.database.immediate_session() as unit:
        unit.delete(unit.get_one(m.WorkspaceMembership, (u.workspace_id, u.alice_id)))
    final = None
    for ordinal in range(ready["batch_count"]):
        claimed = claim(u.bob, u.bh, seeded.run["id"])
        operations = []
        if claimed["task"]["phase"] == "text_repair":
            inspected = body(
                u.bob.post(
                    f"/api/v1/source-cleaning-dispatch-batches/{claimed['batch_id']}/inspect",
                    json={
                        "lease_token": claimed["lease_token"],
                        "action": "search",
                        "arguments": {"query": "narra tive"},
                        "view": "working",
                    },
                    headers=headers(u.bh, "peer-source-inspection"),
                )
            )
            assert "body" in inspected["promoted_block_ids"]
            operations = [
                {
                    "op": "replace_block",
                    "block_id": "body",
                    "replacement": "The narrative begins here, with a complete sentence.",
                    "reason": "Repair the confirmed extraction split.",
                }
            ]
        final = body(
            u.bob.post(
                f"/api/v1/source-cleaning-dispatch-batches/{claimed['batch_id']}/submit",
                json=submission(claimed, operations),
                headers=headers(u.bh, f"peer-source-phase-{ordinal}"),
            )
        )
    assert final is not None and final["finalized"] is True and final["run_status"] == "completed"
    result_id = final["result_artifact_id"]
    with f.case.database.snapshot_session() as unit:
        artifact = unit.get_one(m.Artifact, result_id)
        assert (
            artifact.workspace_id == u.workspace_id
            and artifact.project_id == seeded.record["project_id"]
        )
        assert unit.get_one(m.Project, artifact.project_id).created_by_user_id == u.alice_id
    output = u.bob.get(f"/api/v1/artifacts/{result_id}/content")
    assert output.status_code == 200 and b"The narrative begins here" in output.data
    assert u.alice.get(f"/api/v1/artifacts/{result_id}/content").status_code == 404
    assert (
        u.alice.get(f"/api/v1/source-cleaning-dispatch-runs/{seeded.run['id']}").status_code == 404
    )


def test_create_receipt_rechecks_run_scope_after_json(library, monkeypatch):
    f, u = library, library.u
    seeded = seed(f, u.alice, u.ah, workspace=u.workspace_id)
    token, bearer = scoped_token(
        f.case, u.alice_id, {"app.read", "app.run"}, workspace=u.workspace_id
    )
    client, request_headers = f.case.app.test_client(), headers(bearer, "source-create-current-run")
    accepted = body(client.post(seeded.url, json=seeded.values, headers=request_headers), 202)
    original = Request.get_json

    def parse_then_reduce(self, *args, **kwargs):
        value = original(self, *args, **kwargs)
        if self.path == seeded.url:
            with f.case.database.immediate_session() as unit:
                unit.get_one(m.ApiToken, token.id).scopes_json = ["app.read"]
        return value

    monkeypatch.setattr(Request, "get_json", parse_then_reduce)
    response = client.post(seeded.url, json=seeded.values, headers=request_headers)
    assert response.status_code == 403 and response.headers.get("Idempotency-Replayed") is None
    with f.case.database.snapshot_session() as unit:
        assert unit.get_one(m.SourceCleaningDispatchRun, accepted["id"]).status == "preparing"


@pytest.mark.parametrize("operation", ["renew", "release", "inspect", "submit"])
def test_batch_receipts_recheck_run_after_json_before_replay_or_finalization(
    library, monkeypatch, operation
):
    f, u = library, library.u
    seeded = seed(f, u.alice, u.ah, workspace=u.workspace_id)
    prepare(f, seeded, monkeypatch)
    token, bearer = scoped_token(
        f.case, u.bob_id, {"app.read", "app.run"}, workspace=u.workspace_id
    )
    client = f.case.app.test_client()
    claimed = claim(client, bearer, seeded.run["id"])
    values = {"lease_token": claimed["lease_token"]}
    if operation == "renew":
        values["lease_seconds"] = 900
    elif operation == "inspect":
        values.update(action="search", arguments={"query": "narrative"}, view="working")
    elif operation == "submit":
        values = submission(claimed)
    url = f"/api/v1/source-cleaning-dispatch-batches/{claimed['batch_id']}/{operation}"
    request_headers = headers(bearer, "source-batch-current-" + operation)
    body(client.post(url, json=values, headers=request_headers))
    retry = Mock(side_effect=AssertionError("Denied replay attempted finalization"))
    monkeypatch.setattr(f.services.source_cleaning_dispatch, "retry_finalization_in_session", retry)
    original = Request.get_json

    def parse_then_reduce(self, *args, **kwargs):
        value = original(self, *args, **kwargs)
        if self.path == url:
            with f.case.database.immediate_session() as unit:
                unit.get_one(m.ApiToken, token.id).scopes_json = ["app.read"]
        return value

    monkeypatch.setattr(Request, "get_json", parse_then_reduce)
    response = client.post(url, json=values, headers=request_headers)
    assert response.status_code == 403 and response.headers.get("Idempotency-Replayed") is None
    retry.assert_not_called()


def test_foreign_explicit_input_denies_before_hash_or_preparation(library, monkeypatch):
    f, u = library, library.u
    seeded = seed(f, u.alice, u.ah, workspace=u.workspace_id)
    with f.case.database.snapshot_session() as unit:
        before = unit.scalar(select(func.count()).select_from(m.SourceCleaningDispatchRun))
    native = Mock(side_effect=AssertionError("A foreign input reached source hashing"))
    monkeypatch.setattr("pandrator.web.source_cleaning_dispatch.sha256_file", native)
    response = u.alice.post(
        seeded.url,
        json={"source_artifact_id": f.private.artifact_id},
        headers=headers(u.ah, "source-foreign-explicit-input"),
    )
    assert response.status_code == 404, response.get_json()
    native.assert_not_called()
    with f.case.database.snapshot_session() as unit:
        assert unit.scalar(select(func.count()).select_from(m.SourceCleaningDispatchRun)) == before


@pytest.mark.parametrize("revoked", ["membership", "token"])
def test_denied_queued_worker_records_own_failure_without_blocking_source_changes(
    library, monkeypatch, revoked
):
    f, u = library, library.u
    token, bearer = scoped_token(
        f.case, u.alice_id, {"app.read", "app.run"}, workspace=u.workspace_id
    )
    seeded = seed(
        f,
        u.alice,
        u.ah,
        workspace=u.workspace_id,
        run_client=f.case.app.test_client(),
        run_headers=bearer,
    )
    with f.case.database.immediate_session() as unit:
        if revoked == "membership":
            unit.delete(unit.get_one(m.WorkspaceMembership, (u.workspace_id, u.alice_id)))
        else:
            unit.get_one(m.ApiToken, token.id).revoked_at = m.utcnow()
        before = unit.scalar(select(func.count()).select_from(m.Artifact))
    native = Mock(side_effect=AssertionError("A denied queued job reached native extraction"))
    monkeypatch.setattr(
        "pandrator.web.source_cleaning_dispatch.extract_epub_with_diagnostics", native
    )
    statements = []

    def capture(_connection, _cursor, statement, _parameters, _context, _executemany):
        statements.append(statement)

    event.listen(f.case.database.engine, "before_cursor_execute", capture)
    try:
        assert Worker(
            f.services.jobs, "denied-source-cleaning-worker", f.services.workflow_handlers.handlers()
        ).run_once()
    finally:
        event.remove(f.case.database.engine, "before_cursor_execute", capture)
    assert all("source_cleaning_dispatch_runs.settings_json" not in sql for sql in statements)
    assert f.services.jobs.get(seeded.run["job_id"]).status == "failed"
    native.assert_not_called()
    with f.case.database.snapshot_session() as unit:
        assert unit.get_one(m.SourceCleaningDispatchRun, seeded.run["id"]).status == "failed"
        assert unit.scalar(select(func.count()).select_from(m.Artifact)) == before
    root = f"/api/v1/sessions/{seeded.record['id']}/sources"
    preview = body(u.bob.post(root + "/change-preview", json={"role": "primary"}, headers=u.bh))
    body(
        u.bob.post(
            root + "/change",
            json={
                "role": "primary",
                "new_source_asset_id": None,
                "expected_revision": preview["session_revision"],
                "impact_token": preview["impact_token"],
            },
            headers=headers(u.bh, "source-reset-after-denied-worker"),
        )
    )
