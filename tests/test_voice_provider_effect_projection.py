"""Current members can inspect a safe retained provider receipt through work."""

import json
from types import SimpleNamespace
from typing import Any, cast

from pandrator.web import models as m
from pandrator_mcp.clients.application_work_manager import ApplicationWorkManagerMethods
from pandrator_mcp.context import McpRuntime
from pandrator_mcp.schemas import GetWorkInput
from pandrator_mcp.tools.work import get_work
from tests.test_account_authentication_integration import case as case  # noqa: F401
from tests.test_evidence_access_routes import body
from tests.test_provider_workspace_access import sql_capture
from tests.test_voice_crud_workspace_access import no_network as no_network  # noqa: F401
from tests.test_voice_provider_job_effects import receipt
from tests.test_voice_provider_workspace_http import provider as provider  # noqa: F401
from tests.test_voice_provider_workspace_http import submit


class LocalWorkClient(ApplicationWorkManagerMethods):
    def __init__(self, client):
        self.client = client

    def _request_json(self, path: str, **kwargs: Any) -> dict[str, Any]:
        assert not kwargs or set(kwargs) == {"parameters"}
        return body(self.client.get(path, query_string=kwargs.get("parameters")))

    def _request_binary_json(self, path: str, **_kwargs: Any) -> dict[str, Any]:
        raise AssertionError(f"Work projection must not use binary transport: {path}")


def retained(f):
    queued = body(submit(f), 202)
    value = {**receipt(), "job_id": queued["id"], "workspace_id": f.users.workspace_id,
        "voice_id": f.voice["id"]}
    with f.case.database.immediate_session() as session:
        job = session.get_one(m.Job, queued["id"])
        job.status = "failed"
        job.error_code = "provider_local_not_applied"
        job.error_message = "The provider completed; local publication was stopped. Receipt retained."
        job.external_effect_json = value
    return queued["id"], value


def test_protected_http_and_real_mcp_work_builder_keep_only_safe_receipt(provider):
    f = provider
    job_id, raw = retained(f)
    expected = {key: raw[key] for key in
        ("operation", "state", "service_id", "provider_voice_id", "binding_state")}
    diagnostic = body(f.users.alice.get(f"/api/v1/jobs/{job_id}"))
    public = body(f.users.alice.get(f"/api/v1/work/{job_id}"))
    assert diagnostic["external_effect"] == public["external_effect"] == expected
    assert "payload_json" not in public and "external_effect_json" not in diagnostic
    app = LocalWorkClient(f.users.alice)
    runtime = cast(McpRuntime, SimpleNamespace(require_application=lambda: app))
    outcome = get_work(runtime, GetWorkInput(work_id=job_id, work_type="job"))
    assert outcome.result["external_effect"] == expected
    assert outcome.work is not None and outcome.work.state == "failed"
    encoded = json.dumps(outcome.result)
    assert all(raw[key] not in encoded for key in ("token", "request_sha256", "baseline_sha256"))
    assert "lease_generation" not in encoded
    with f.case.database.immediate_session() as session:
        session.delete(session.get_one(m.WorkspaceMembership, (f.users.workspace_id, f.users.alice_id)))
    for root in ("jobs", "work"):
        assert f.users.alice.get(f"/api/v1/{root}/{job_id}").status_code == 404
        assert body(f.users.bob.get(f"/api/v1/{root}/{job_id}"))["external_effect"] == expected


def test_work_list_reads_receipts_without_row_queries_or_raw_inputs(provider):
    f = provider
    job_id, _raw = retained(f)
    with sql_capture(f.case.database) as statements:
        result = body(f.users.alice.get("/api/v1/work"))
    row = next(item for item in result["items"] if item["id"] == job_id)
    assert row["external_effect"]["binding_state"] == "not_applied"
    reads = [statement for statement, _parameters in statements if
        "jobs.external_effect_json" in statement and statement.lstrip().upper().startswith("SELECT")]
    assert len(reads) == 1
    assert "jobs.payload_json" not in reads[0]


def test_absent_unrelated_and_malformed_effects_add_no_public_field(provider):
    f = provider
    job_id = body(submit(f), 202)["id"]
    for value, kind in ((None, "voice.publish"), ({"token": "private"}, "voice.publish"), (receipt(), "noop")):
        with f.case.database.immediate_session() as session:
            job = session.get_one(m.Job, job_id)
            job.external_effect_json = value
            job.kind = kind
        for root in ("jobs", "work"):
            assert "external_effect" not in body(f.users.alice.get(f"/api/v1/{root}/{job_id}"))
