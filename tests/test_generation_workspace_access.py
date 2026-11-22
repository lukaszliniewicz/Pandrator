"""Project content checks precede parsing, replay, and generation commits."""

from __future__ import annotations

from copy import deepcopy
from types import SimpleNamespace

from sqlalchemy import event, func, select

from pandrator.web import models as m
from tests.test_evidence_access_routes import body, headers
from tests.test_google_design_workspace_http import MODEL
from tests.test_google_design_workspace_http import case as case  # noqa: F401
from tests.test_google_design_workspace_http import design as design  # noqa: F401
from tests.test_google_design_workspace_http import no_network as no_network  # noqa: F401


def project(f, *, shared=False):
    client, csrf = (f.users.alice, f.users.ah) if shared else (f.users.owner, f.users.oh)
    value = {"name": "Generation access fixture", "workflow_kind": "voiceover"}
    if shared:
        value["workspace_id"] = f.users.workspace_id
    source = body(client.post("/api/v1/sessions", json=value, headers=csrf), 201)
    plan = f.services.generation.create_plan(
        source["id"], source_revision_id=None, settings={}, segments=[{"text": "Reviewed text."}]
    )
    segment = f.services.generation.list_segments(source["id"])["items"][0]
    with f.case.database.immediate_session() as session:
        run = m.GenerationRun(
            session_id=source["id"],
            plan_revision_id=plan["active_revision_id"],
            status="completed",
            settings_snapshot_json={"tts": {"model": MODEL, "voice": "Kore"}},
        )
        session.add(run)
        session.flush()
        run_id = run.id
    return SimpleNamespace(source=source, plan=plan, segment=segment, run_id=run_id)


def requests_for(p):
    prefix = f"/api/v1/sessions/{p.source['id']}"
    return [
        ("get", prefix + "/generation/summary"),
        ("get", prefix + "/generation-plan/revisions"),
        ("get", prefix + f"/generation-plan/revisions/{p.plan['active_revision_id']}"),
        ("get", prefix + "/generation-segments"),
        ("get", prefix + "/generation-runs/latest"),
        ("get", prefix + "/generation-runs?limit=invalid"),
        ("get", f"/api/v1/generation-runs/{p.run_id}"),
        ("get", prefix + "/output-assemblies/latest"),
        ("get", prefix + "/export-audio"),
        ("get", prefix + "/generation-controls"),
        ("get", prefix + "/settings/tts"),
        ("post", prefix + "/settings/resolve"),
        ("put", prefix + "/settings/tts"),
        ("patch", prefix + "/settings/tts"),
        ("post", prefix + "/generation-plan"),
        ("post", prefix + "/generation-plan/topology"),
        ("post", prefix + "/generation-plan/topology/batch"),
        ("patch", prefix + "/generation-segments"),
        ("patch", f"/api/v1/generation-segments/{p.segment['id']}"),
        ("post", f"/api/v1/generation-segments/{p.segment['id']}/takes/unknown/select"),
        ("post", prefix + "/generation-runs"),
        ("post", prefix + "/generation-admissions"),
        ("post", prefix + "/generation-runs/preview"),
        ("post", f"/api/v1/generation-runs/{p.run_id}/pause"),
        ("post", f"/api/v1/generation-runs/{p.run_id}/resume"),
        ("post", f"/api/v1/generation-runs/{p.run_id}/repair-timing"),
        ("post", f"/api/v1/generation-runs/{p.run_id}/cancel"),
        ("delete", f"/api/v1/generation-runs/{p.run_id}"),
        ("post", prefix + "/output-assemblies"),
        ("put", prefix + "/generation-controls"),
        ("post", prefix + "/generation-recast/preview"),
        ("post", prefix + "/generation-recast"),
        ("post", f"/api/v1/generation-runs/{p.run_id}/audio-version/undo-preview"),
        ("post", f"/api/v1/generation-runs/{p.run_id}/audio-version/undo"),
    ]


def counts(f):
    with f.case.database.snapshot_session() as session:
        return tuple(
            session.scalar(select(func.count()).select_from(model))
            for model in (m.Job, m.ApiIdempotency, m.SessionSetting, m.GenerationRun)
        )


def test_foreign_route_family_denies_before_protected_reads_parsing_or_receipts(design):
    f, statuses, statements = design, {}, []
    p = project(f)
    before = counts(f)

    def capture(_c, _cu, statement, _p, _co, _e):
        statements.append(statement)

    event.listen(f.case.database.engine, "before_cursor_execute", capture)
    try:
        for method, path in requests_for(p):
            response = getattr(f.users.bob, method)(
                path,
                json={"invalid": "body"},
                headers=headers(f.users.bh, "foreign-generation-route"),
            )
            statuses[(method, path)] = response.status_code
    finally:
        event.remove(f.case.database.engine, "before_cursor_execute", capture)
    assert set(statuses.values()) == {404}, statuses
    assert counts(f) == before
    assert not any(
        any(
            column in sql
            for column in (
                "settings_snapshot_json",
                "generation_segments.text",
                "knowledge_ledgers.payload_json",
                "session_settings.value_json",
            )
        )
        for sql in statements
    )


def test_member_reads_and_edits_another_members_project(design):
    f = design
    p = project(f, shared=True)
    prefix = f"/api/v1/sessions/{p.source['id']}"
    for suffix in (
        "generation/summary",
        "generation-plan/revisions",
        "generation-segments",
        "generation-runs/latest",
        "generation-runs",
        "generation-controls",
        "settings/tts",
    ):
        body(f.users.bob.get(f"{prefix}/{suffix}"))
    # Naming the export's audio version is a read: a member sees it, and with
    # no assembly it names none.
    assert body(f.users.bob.get(f"{prefix}/export-audio")) == {
        "status": "none",
        "version": None,
        "candidates": [],
    }
    body(f.users.bob.get(f"/api/v1/generation-runs/{p.run_id}"))
    body(
        f.users.bob.put(
            prefix + "/generation-controls",
            json={"expected_revision": 0, "characters": [], "cast": {}},
            headers=headers(f.users.bh, "member-cast-edit"),
        )
    )
    response = body(
        f.users.bob.patch(
            prefix + "/settings/tts",
            json={"value": {"voice": "Kore"}},
            headers={**headers(f.users.bh, "member-speech-edit"), "If-Match": '"0"'},
        )
    )
    assert response["override"]["voice"] == "Kore"
    response = body(
        f.users.bob.patch(
            f"/api/v1/generation-segments/{p.segment['id']}",
            json={"text": "Edited by the second Member."},
            headers={**f.users.bh, "If-Match": f'"{p.segment["revision"]}"'},
        )
    )
    assert response["text"] == "Edited by the second Member."


def test_can_view_reads_content_but_cannot_edit_process_or_cancel(design):
    f = design
    p = project(f)
    viewers_path = f"/api/v1/projects/{p.source['project_id']}/viewers"
    current = body(f.users.owner.get(viewers_path))
    body(
        f.users.owner.put(
            f"{viewers_path}/{f.users.alice_id}",
            json={"expected_revision": current["revision"]},
            headers=f.users.oh,
        )
    )
    prefix = f"/api/v1/sessions/{p.source['id']}"
    body(f.users.alice.get(prefix + "/generation-runs/latest"))
    body(f.users.alice.get(f"/api/v1/generation-runs/{p.run_id}"))
    body(f.users.alice.get(prefix + "/generation-controls"))
    body(f.users.alice.get(prefix + "/settings/tts"))
    # Naming the export's audio version is a read, so a can-view user has it.
    assert body(f.users.alice.get(prefix + "/export-audio"))["status"] == "none"
    before = counts(f)
    for method, path in requests_for(p):
        if method == "get" or path.endswith(
            ("/settings/resolve", "/undo-preview", "/generation-recast/preview")
        ):
            continue
        response = getattr(f.users.alice, method)(
            path, json={"invalid": "body"}, headers=headers(f.users.ah, "viewer-generation-route")
        )
        assert response.status_code == 403, (method, path, response.status_code)
    assert counts(f) == before


def test_membership_loss_precedes_settings_receipt_replay(design):
    f = design
    p = project(f, shared=True)
    path = f"/api/v1/sessions/{p.source['id']}/settings/tts"
    options = {
        "json": {"value": {"voice": "Kore"}},
        "headers": {**headers(f.users.ah, "settings-membership-replay"), "If-Match": '"0"'},
    }
    first = body(f.users.alice.put(path, **options))
    assert first["revision"] == 1
    before = counts(f)
    with f.case.database.immediate_session() as session:
        session.delete(
            session.get_one(m.WorkspaceMembership, (f.users.workspace_id, f.users.alice_id))
        )
    response = f.users.alice.put(path, **options)
    assert response.status_code == 404
    assert response.headers.get("Idempotency-Replayed") != "true"
    assert counts(f) == before


def test_membership_loss_after_preparation_blocks_run_commit(design, monkeypatch):
    f = design
    p = project(f, shared=True)
    prepare = f.services.generation.prepare_start
    calls = []

    def prepare_then_revoke(*args, **kwargs):
        result = prepare(*args, **kwargs)
        calls.append(True)
        with f.case.database.immediate_session() as session:
            session.delete(
                session.get_one(m.WorkspaceMembership, (f.users.workspace_id, f.users.alice_id))
            )
        return result

    monkeypatch.setattr(f.services.generation, "prepare_start", prepare_then_revoke)
    before = counts(f)
    response = f.users.alice.post(
        f"/api/v1/sessions/{p.source['id']}/generation-runs",
        json={
            "run_override": {
                "tts": {
                    "service": "gemini",
                    "model": MODEL,
                    "xtts_model": MODEL,
                    "voice": "Kore",
                    "speaker": "Kore",
                }
            }
        },
        headers=headers(f.users.ah, "generation-prepare-role-loss"),
    )
    assert calls and response.status_code == 404, response.get_json()
    assert counts(f) == before


def test_project_responses_hide_connections_without_rewriting_frozen_settings(design):
    f = design
    p = project(f, shared=True)
    private = {
        "service": "gemini",
        "model": MODEL,
        "xtts_model": MODEL,
        "voice": "Kore",
        "generation_prompt": "Read the URL https://example.com aloud.",
        "provider_configs": [
            {
                "id": "gemini",
                "api_base": "https://private-model.invalid",
                "secret_ref": "db:private-model-key",
                "api_key_env": "PRIVATE_MODEL_ENV",
            }
        ],
        "service_configs": [{"id": "shadow", "api_base": "https://private-shadow.invalid"}],
        "audio_cpp_base_url": "http://private-host:1234",
        "api_key_env": "PRIVATE_OTHER_ENV",
        "openai_audio_endpoint": "existing_connection",
        "secret_ref": "db:private-reference",
    }
    frozen = {"tts": deepcopy(private)}
    with f.case.database.immediate_session() as session:
        session.get_one(m.GenerationRun, p.run_id).settings_snapshot_json = frozen
        setting = session.get_one(m.SessionSetting, (p.source["id"], "tts"))
        setting.revision = 1
        setting.value_json = deepcopy(private)
    prefix = f"/api/v1/sessions/{p.source['id']}"
    responses = [
        body(f.users.bob.get(prefix + "/generation-runs/latest"))["item"]["settings_snapshot"][
            "tts"
        ],
        body(f.users.bob.get(prefix + "/generation-runs"))["items"][0]["settings_snapshot"]["tts"],
        body(f.users.bob.get(f"/api/v1/generation-runs/{p.run_id}"))["settings_snapshot"]["tts"],
        body(f.users.bob.get(prefix + "/settings/tts"))["override"],
        body(
            f.users.bob.post(
                prefix + "/settings/resolve", json={"sections": ["tts"]}, headers=f.users.bh
            )
        )["value"]["tts"],
    ]
    for public in responses:
        assert public["model"] == MODEL and public["voice"] == "Kore"
        assert public["generation_prompt"] == private["generation_prompt"]
        assert public["openai_audio_endpoint"] == "existing_connection"
        assert not set(public) & {
            "provider_configs",
            "service_configs",
            "api_key_env",
            "audio_cpp_base_url",
            "secret_ref",
        }
    with f.case.database.snapshot_session() as session:
        assert session.get_one(m.GenerationRun, p.run_id).settings_snapshot_json == frozen
        assert session.get_one(m.SessionSetting, (p.source["id"], "tts")).value_json == private


def test_member_project_overrides_select_connections_without_configuring_them(design):
    f = design
    p = project(f, shared=True)
    prefix = f"/api/v1/sessions/{p.source['id']}"
    before = counts(f)
    for layer in (
        {"audio_cpp_base_url": "https://unapproved.invalid"},
        {"provider_configs": []},
        {"service_configs": []},
        {"secret_ref": "db:unapproved"},
        {"openai_audio_endpoint": {"base_url": "https://unapproved.invalid"}},
        {"openai_audio_endpoint": "https://unapproved.invalid"},
        {"audio_cpp_model_settings": {"model": {"api_key_env": "HOST_CREDENTIAL"}}},
        {"preview_service_id": "audio_cpp"},
        {"service_id": "kobold_qwen"},
        {"preview_adapter": "openai_compatible"},
        {"preview_api_base": "https://unapproved.invalid"},
    ):
        for method in ("put", "patch"):
            response = getattr(f.users.alice, method)(
                prefix + "/settings/tts",
                json={"value": layer},
                headers={**headers(f.users.ah, "project-connection-settings"), "If-Match": '"0"'},
            )
            assert response.status_code == 422, response.get_json()
        for suffix in ("generation-runs", "generation-admissions", "generation-runs/preview"):
            response = f.users.alice.post(
                prefix + "/" + suffix,
                json={"run_override": {"tts": layer}},
                headers=headers(f.users.ah, "project-connection-run"),
            )
            assert response.status_code == 422, (suffix, response.get_json())
        assert counts(f) == before


def add_assembly(f, p, name, *, run_id, plan_revision_id, created_at):
    from pandrator.web.projects.artifact_identity import resolve_artifact_ownership

    with f.case.database.immediate_session() as session:
        ownership = resolve_artifact_ownership(session, session_id=p.source["id"])
        artifact = m.Artifact(
            session_id=p.source["id"],
            workspace_id=ownership.workspace_id,
            project_id=ownership.project_id,
            kind="audio",
            role="assembled_audio",
            relative_path=f"{name}.wav",
            content_hash=name.ljust(64, "0"),
            state="current",
        )
        session.add(artifact)
        session.flush()
        session.add(
            m.OutputAssembly(
                session_id=p.source["id"],
                generation_run_id=run_id,
                status="completed",
                artifact_id=artifact.id,
                settings_json={"plan_revision_id": plan_revision_id},
                created_at=created_at,
            )
        )


def test_export_audio_route_matches_its_published_schema(design):
    from datetime import UTC, datetime

    from pandrator.web.openapi import build_openapi_document

    f = design
    p = project(f)
    path = f"/api/v1/sessions/{p.source['id']}/export-audio"
    document = build_openapi_document()
    schema = document["paths"]["/api/v1/sessions/{sessionId}/export-audio"]["get"][
        "responses"
    ]["200"]["content"]["application/json"]["schema"]

    def check(payload, node):
        assert set(payload) == set(node["required"]) == set(node["properties"])
        version = node["properties"]["version"]["anyOf"][0]
        for item in [payload["version"], *payload["candidates"]]:
            if item is not None:
                assert set(item) == set(version["required"]) == set(version["properties"])

    revision = p.plan["active_revision_id"]
    add_assembly(
        f, p, "first", run_id=p.run_id, plan_revision_id=revision,
        created_at=datetime(2026, 11, 21, 10, 0, tzinfo=UTC),
    )
    named = body(f.users.owner.get(path))
    assert named["status"] == "named" and named["candidates"] == []
    assert named["version"]["generation_run_id"] == p.run_id
    assert named["version"]["active_revision"] is True
    check(named, schema)

    # A second assembly on the active revision too: neither is singled out.
    add_assembly(
        f, p, "second", run_id=None, plan_revision_id=revision,
        created_at=datetime(2026, 11, 21, 11, 0, tzinfo=UTC),
    )
    ambiguous = body(f.users.owner.get(path))
    assert ambiguous["status"] == "ambiguous" and ambiguous["version"] is None
    assert [item["generation_run_id"] for item in ambiguous["candidates"]] == [None, p.run_id]
    check(ambiguous, schema)
